"""/metrics (Prometheus 형식): 인증 없이 열려 있고, 토큰·개인정보·DB 주소가 지표에 들어가지 않는다."""
import os


def test_metrics_is_prometheus_text_without_secrets(client):
    client.get("/api/v1/departments", headers={"Authorization": "Bearer secret-token-value?x=010-1234-5678"})
    client.get("/healthz")
    m = client.get("/metrics")
    assert m.status_code == 200
    assert "text/plain" in m.headers["content-type"]
    assert "http_requests_total" in m.text
    for hidden in ("secret-token-value", "010-1234-5678", "Bearer", os.environ["DB_URL"], os.environ["JWT_SECRET"]):
        assert hidden not in m.text
