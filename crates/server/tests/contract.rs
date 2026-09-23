use axum::{body::{Body,to_bytes}, http::Request};
use tower::ServiceExt;

#[tokio::test]
async fn health_reports_real_local_service() {
    let response = hatshy_server::app().oneshot(Request::builder().uri("/api/health").body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(response.status(), 200, "a fresh install must expose a health endpoint");
    let body = to_bytes(response.into_body(), 4096).await.unwrap();
    let value: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(value["status"], "ok");
    assert_eq!(value["app"], "AI Hatshy");
}
