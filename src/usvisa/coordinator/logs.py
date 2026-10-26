"""Node diagnostics, ownership-filtered browsing and timeline projections."""
import json
from datetime import datetime, timedelta, timezone

from usvisa.protocol.messages import LogEntry, timestamp


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


def store(work, node_id, entry: LogEntry, received_at):
    from .repository import _ticks
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
    return result


def browse(repository, *, owner_id, hours=24, node_id=None, view="activity", before=None, limit=100):
    from .repository import _ticks
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    where, params = ["l.occurred_at>=?"], [_ticks(since)]
    if owner_id:
        where.append("p.owner_id=?")
        params.append(owner_id)
    if node_id:
        where.append("l.node_id=?")
        params.append(str(node_id))
    if view == "activity":
        where.append("l.category IN ('poll','finding')")
    elif view == "errors":
        where.append("l.level='error'")
    elif view == "reschedule":
        where.append("l.log_group='reschedule'")
    clause = " WHERE " + " AND ".join(where)
    with repository.transaction() as work:
        db = work.connection
        total = db.execute("SELECT COUNT(*) FROM (" + _SELECT + clause + ")", params).fetchone()[0]
        points = db.execute(_SELECT + clause + " ORDER BY l.occurred_at DESC,l.id DESC LIMIT 3000", params).fetchall()
        listing, listing_params = clause, list(params)
        if before:
            cursor = db.execute("SELECT occurred_at FROM node_logs WHERE id=?", (before,)).fetchone()
            if cursor:
                listing += " AND (l.occurred_at<? OR (l.occurred_at=? AND l.id<?))"
                listing_params.extend((cursor[0], cursor[0], before))
            else:
                listing += " AND 0"
        rows = db.execute(_SELECT + listing + " ORDER BY l.occurred_at DESC,l.id DESC LIMIT ?",
                          [*listing_params, limit + 1]).fetchall()
        nodes = db.execute("SELECT n.node_id,p.name,u.username,n.active FROM nodes n JOIN portal_nodes p USING(node_id) "
                           "JOIN users u ON u.user_id=p.owner_id" + (" WHERE p.owner_id=?" if owner_id else "") +
                           " ORDER BY u.username,p.name", (owner_id,) if owner_id else ()).fetchall()
    return {"events": [item(row) for row in rows[:limit]], "points": [item(row, compact=True) for row in reversed(points)],
            "total": total, "chartLimited": total > 3000, "nextCursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            "from": timestamp(since), "to": timestamp(now),
            "nodes": [{"nodeId": row["node_id"], "name": row["name"], "owner": row["username"], "active": bool(row["active"])} for row in nodes]}


def detail(repository, event_id: int, *, owner_id):
    clause, params = " WHERE l.id=?", [event_id]
    if owner_id:
        clause += " AND p.owner_id=?"
        params.append(owner_id)
    with repository.transaction() as work:
        row = work.connection.execute(_SELECT + clause, params).fetchone()
        if row is None:
            return None
        trace = work.connection.execute(_SELECT + " WHERE l.node_id=? AND l.trace_id=? ORDER BY l.occurred_at,l.id LIMIT 250",
                                        (row["node_id"], row["trace_id"])).fetchall() if row["trace_id"] else [row]
    return {"event": item(row), "trace": [item(part) for part in trace]}
