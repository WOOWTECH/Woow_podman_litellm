# Podman variant of the reviewed HA/PaaS LiteLLM 1.104.0 source.
# PostgreSQL stays in its own rootless Quadlet container.
FROM ghcr.io/berriai/litellm-non_root:1.104.0@sha256:37066242e6b8327fdd627682a66a9ae2a599553b4d5a84c905c6d8b9eb35c01e
USER 0
COPY patches/ /opt/woow/patches/
RUN set -eu; \
    for patch in apply_responses_fix apply_subscription_policy apply_api_compat \
      apply_routes_auth apply_security_headers apply_login_throttle \
      apply_api_errors apply_upstream_guard apply_m4_batch1 \
      apply_stream_slot_release apply_chatgpt_system_role; do \
      python -I "/opt/woow/patches/${patch}.py"; \
    done
COPY overlay/ /opt/woow/overlay/
RUN python -I /opt/woow/overlay/wheel_overlay.py apply \
      --lock /opt/woow/overlay/overlay.lock.json --wheels /opt/woow/overlay/wheels \
 && python -I /opt/woow/overlay/wheel_overlay.py verify \
      --lock /opt/woow/overlay/overlay.lock.json
COPY plugin/ /opt/woow/plugin/
COPY start.sh /opt/woow/start.sh
COPY db_prepare.py /opt/woow/db_prepare.py
RUN chmod 0555 /opt/woow/start.sh && chmod -R a+rX /opt/woow/plugin /opt/woow/patches
USER 65534
ENTRYPOINT ["/opt/woow/start.sh"]
CMD ["--port", "4000"]
