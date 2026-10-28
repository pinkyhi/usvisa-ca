"""Node diagnostics, ownership-filtered browsing and timeline projections."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from usvisa.protocol.messages import LogEntry, timestamp
from usvisa.shared.bodylogs import BODY_EVENTS, assemble


RETENTION = timedelta(days=7)


def retention_indexes(db):
    db.execute("CREATE INDEX IF NOT EXISTS logs_time ON node_logs(occurred_at)")
    db.execute("CREATE INDEX IF NOT EXISTS findings_time ON findings(observed_at)")
    db.execute("CREATE INDEX IF NOT EXISTS inbox_time ON inbox(processed_at)")
    db.execute("CREATE INDEX IF NOT EXISTS outbox_time ON outbox(created_at)")


def prune(work, now):
    """Expire diagnostic/business history while preserving pending deliveries."""
    from .repository import _ticks
    cutoff = _ticks(now - RETENTION)
    db = work.connection
    db.execute("DELETE FROM node_logs WHERE occurred_at<?", (cutoff,))
    db.execute("DELETE FROM log_body_parts WHERE occurred_at<?", (cutoff,))
    db.execute("DELETE FROM findings WHERE observed_at<?", (cutoff,))
    db.execute("DELETE FROM outbox WHERE created_at<? AND status!='pending'", (cutoff,))
    db.execute("DELETE FROM inbox WHERE processed_at<? AND NOT EXISTS "
               "(SELECT 1 FROM findings f WHERE f.node_id=inbox.node_id AND f.message_id=inbox.message_id)", (cutoff,))


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS node_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
        node_id TEXT NOT NULL REFERENCES nodes(node_id), occurred_at INTEGER NOT NULL,
        received_at INTEGER NOT NULL, category TEXT NOT NULL, level TEXT NOT NULL,
        event TEXT NOT NULL, trace_id TEXT, log_group TEXT, details TEXT NOT NULL,
        UNIQUE(node_id,event_id))""")
    db.execute("CREATE INDEX IF NOT EXISTS logs_node_time ON node_logs(node_id,occurred_at DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS logs_trace ON node_logs(node_id,trace_id,occurred_at)")
    db.execute("CREATE INDEX IF NOT EXISTS logs_category_time ON node_logs(category,occurred_at DESC)")
    for row in db.execute("SELECT * FROM findings").fetchall():
        db.execute("INSERT OR IGNORE INTO node_logs(event_id,node_id,occurred_at,received_at,category,level,event,details) "
                   "VALUES(?,?,?,?, 'finding','info','slots_found',?)",
                   (row["message_id"], row["node_id"], row["observed_at"], row["observed_at"],
                    json.dumps({"dates": json.loads(row["dates"]), "count": len(json.loads(row["dates"]))})))


def migrate_bodies(db):
    db.execute("""CREATE TABLE IF NOT EXISTS log_body_parts(
        node_id TEXT NOT NULL REFERENCES nodes(node_id), event_id TEXT NOT NULL,
        part_index INTEGER NOT NULL, part_count INTEGER NOT NULL, digest TEXT NOT NULL,
        metadata TEXT NOT NULL, occurred_at INTEGER NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY(node_id,event_id,part_index))""")
    db.execute("CREATE INDEX IF NOT EXISTS log_body_time ON log_body_parts(occurred_at)")


def _body_entry(work, node_id, entry):
    """Reassemble transactionally; parts never become extra portal trace events."""
    from .repository import _ticks
    db = work.connection
    identity = (str(node_id), str(entry.event_id))
    if db.execute("SELECT 1 FROM node_logs WHERE node_id=? AND event_id=?", identity).fetchone():
        return None
    part = entry.details
    metadata = json.dumps([timestamp(entry.occurred_at), entry.category, entry.level, entry.event,
                           str(entry.trace_id) if entry.trace_id else None, entry.log_group])
    previous = db.execute("SELECT * FROM log_body_parts WHERE node_id=? AND event_id=? LIMIT 1", identity).fetchone()
    if previous and (previous["digest"] != part["chunk_sha256"] or previous["part_count"] != part["chunk_count"]
                     or previous["metadata"] != metadata):
        return None
    db.execute("INSERT OR IGNORE INTO log_body_parts VALUES(?,?,?,?,?,?,?,?)",
               (*identity, part["chunk_index"], part["chunk_count"], part["chunk_sha256"], metadata,
                _ticks(entry.occurred_at), part["body_chunk"]))
    rows = db.execute("SELECT body FROM log_body_parts WHERE node_id=? AND event_id=? ORDER BY part_index", identity).fetchall()
    if len(rows) != part["chunk_count"]:
        return None
    try:
        details = assemble([row["body"] for row in rows], part["chunk_sha256"])
    except (ValueError, UnicodeError):
        db.execute("DELETE FROM log_body_parts WHERE node_id=? AND event_id=?", identity)
        return None
    db.execute("DELETE FROM log_body_parts WHERE node_id=? AND event_id=?", identity)
    return replace(entry, details=details)


def store(work, node_id, entry: LogEntry, received_at):
    from .repository import _ticks
    if entry.occurred_at < received_at - RETENTION:
        return
    if entry.event in BODY_EVENTS:
        entry = _body_entry(work, node_id, entry)
        if entry is None:
            return
    work.connection.execute(
        "INSERT OR IGNORE INTO node_logs(event_id,node_id,occurred_at,received_at,category,level,event,trace_id,log_group,details) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (str(entry.event_id), str(node_id), _ticks(entry.occurred_at), _ticks(received_at), entry.category,
         entry.level, entry.event, str(entry.trace_id) if entry.trace_id else None, entry.log_group,
         json.dumps(entry.details, ensure_ascii=True)))


_SELECT = """SELECT l.*,p.name AS node_name,u.username AS owner FROM node_logs l
 JOIN portal_nodes p ON p.node_id=l.node_id JOIN users u ON u.user_id=p.owner_id"""


def item(row, *, compact=False):
    from .repository import _datetime
    details = json.loads(row["details"])
    result = {"id": row["id"], "nodeId": row["node_id"], "nodeName": row["node_name"], "owner": row["owner"],
              "occurredAt": timestamp(_datetime(row["occurred_at"])), "category": row["category"],
              "level": row["level"], "event": row["event"], "traceId": row["trace_id"], "logGroup": row["log_group"]}
    if compact:
        result["count"] = details.get("count", len(details.get("dates", [])))
        result["earliest"] = details.get("earliest") or next(iter(details.get("dates", [])), None)
        result["status"] = details.get("status")
    else:
        result["details"] = details
        result["receivedAt"] = timestamp(_datetime(row["received_at"]))
    if "trace_size" in row.keys():
        result.update(isTrace=True, stepCount=row["trace_size"])
    return result


def browse(repository, *, owner_id, hours=24, node_id=None, view="activity", before=None, limit=100, event_type=None):
    from .repository import _ticks
    now = datetime.now(timezone.utc)
    since = now - min(timedelta(hours=hours), RETENTION)
    where, params = ["l.occurred_at>=?"], [_ticks(since)]
    if owner_id:
        where.append("p.owner_id=?")
        params.append(owner_id)
    if node_id:
        where.append("l.node_id=?")
        params.append(str(node_id))
    scope = _SELECT + " WHERE " + " AND ".join(where)
    if view == "reschedule":
        # Group before filtering/pagination so one trace can never straddle pages.
        # Legacy peers use the times/POST events; new peers mark book() entry too,
        # including failures while preparing a form, before any HTTP request.
        query = """WITH scoped AS (""" + scope + """), eligible AS (
          SELECT DISTINCT s.node_id,s.trace_id FROM scoped s
          WHERE s.trace_id IS NOT NULL AND s.log_group='reschedule'
          AND (s.event IN ('times_requested','times_response','booking_no_times','booking_post_started',
                          'booking_post_outcome','booking_verification','booking_blocked')
               OR (s.event IN ('booking_attempt_started','booking_prepared')
                   AND COALESCE(json_extract(s.details,'$.dry_run'),0)=0))
          AND NOT EXISTS (SELECT 1 FROM scoped dry WHERE dry.node_id=s.node_id AND dry.trace_id=s.trace_id
            AND (dry.event='booking_dry_run' OR (dry.event IN ('booking_attempt_started','booking_prepared')
                 AND json_extract(dry.details,'$.dry_run')=1)))
        ), steps AS (
          SELECT s.* FROM scoped s JOIN eligible e ON e.node_id=s.node_id AND e.trace_id=s.trace_id
          WHERE s.log_group='reschedule'
        ), ranked AS (
          SELECT *, COUNT(*) OVER (PARTITION BY node_id,trace_id) AS trace_size,
            ROW_NUMBER() OVER (PARTITION BY node_id,trace_id ORDER BY
              CASE level WHEN 'error' THEN 2 WHEN 'warning' THEN 1 ELSE 0 END DESC,
              occurred_at DESC,id DESC) AS trace_rank FROM steps
        ), records AS (SELECT * FROM ranked WHERE trace_rank=1) """
        event_filter = (" WHERE EXISTS (SELECT 1 FROM steps s WHERE s.node_id=l.node_id "
                        "AND s.trace_id=l.trace_id AND s.event=?)") if event_type else " WHERE 1"
        types_source = "steps"
    else:
        condition = "category IN ('poll','finding')" if view == "activity" else "level='error'"
        query = "WITH records AS (" + scope + " AND " + condition + ") "
        event_filter = " WHERE l.event=?" if event_type else " WHERE 1"
        types_source = "records"
    filtered_params = [*params, *([event_type] if event_type else [])]
    with repository.transaction() as work:
        prune(work, now)
        db = work.connection
        event_types = [row[0] for row in db.execute(query + "SELECT DISTINCT event FROM " + types_source + " ORDER BY event", params)]
        records = query + "SELECT * FROM records l" + event_filter
        total = db.execute(query + "SELECT COUNT(*) FROM records l" + event_filter, filtered_params).fetchone()[0]
        points = db.execute(records + " ORDER BY l.occurred_at DESC,l.id DESC LIMIT 3000", filtered_params).fetchall()
        listing, listing_params = "", list(filtered_params)
        if before:
            cursor = db.execute("SELECT occurred_at FROM node_logs WHERE id=?", (before,)).fetchone()
            if cursor:
                listing += " AND (l.occurred_at<? OR (l.occurred_at=? AND l.id<?))"
                listing_params.extend((cursor[0], cursor[0], before))
            else:
                listing += " AND 0"
        rows = db.execute(records + listing + " ORDER BY l.occurred_at DESC,l.id DESC LIMIT ?",
                          [*listing_params, limit + 1]).fetchall()
        nodes = db.execute("SELECT n.node_id,p.name,u.username,n.active FROM nodes n JOIN portal_nodes p USING(node_id) "
                           "JOIN users u ON u.user_id=p.owner_id" + (" WHERE p.owner_id=?" if owner_id else "") +
                           " ORDER BY u.username,p.name", (owner_id,) if owner_id else ()).fetchall()
    return {"events": [item(row) for row in rows[:limit]], "points": [item(row, compact=True) for row in reversed(points)],
            "total": total, "chartLimited": total > 3000, "nextCursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            "from": timestamp(since), "to": timestamp(now), "eventTypes": event_types,
            "nodes": [{"nodeId": row["node_id"], "name": row["name"], "owner": row["username"], "active": bool(row["active"])} for row in nodes]}


def detail(repository, event_id: int, *, owner_id):
    from .repository import _ticks
    now = datetime.now(timezone.utc)
    cutoff = _ticks(now - RETENTION)
    clause, params = " WHERE l.id=? AND l.occurred_at>=?", [event_id, cutoff]
    if owner_id:
        clause += " AND p.owner_id=?"
        params.append(owner_id)
    with repository.transaction() as work:
        prune(work, now)
        row = work.connection.execute(_SELECT + clause, params).fetchone()
        if row is None:
            return None
        trace = work.connection.execute(_SELECT + " WHERE l.node_id=? AND l.trace_id=? AND l.occurred_at>=? ORDER BY l.occurred_at,l.id LIMIT 250",
                                        (row["node_id"], row["trace_id"], cutoff)).fetchall() if row["trace_id"] else [row]
    return {"event": item(row), "trace": [item(part) for part in trace]}
