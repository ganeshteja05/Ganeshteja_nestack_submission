from flask import Flask, jsonify

from ratelimiter import init_rate_limiter, rate_limit


def create_app(store=None):
    app = Flask(__name__)
    init_rate_limiter(app, store)

    @app.post("/ai/generate")
    @rate_limit("ai")
    def generate():
        return jsonify({"ok": True})

    @app.post("/ai/summarise")
    @rate_limit("ai")
    def summarise():
        return jsonify({"ok": True})

    @app.get("/data/list")
    @rate_limit("read")
    def data_list():
        return jsonify({"ok": True})

    @app.get("/data/export")
    @rate_limit("read")
    def data_export():
        return jsonify({"ok": True})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
