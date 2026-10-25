FROM rabbitmq:4.2-management
RUN rabbitmq-plugins enable --offline rabbitmq_web_stomp
COPY rabbitmq.conf /etc/rabbitmq/rabbitmq.conf
COPY --chmod=755 entrypoint.sh /usr/local/bin/usvisa-rabbitmq
ENTRYPOINT ["/usr/local/bin/usvisa-rabbitmq"]
CMD ["rabbitmq-server"]
