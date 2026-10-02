"""`openachievements-server`: run the sync server (see server/app.py for settings)."""
import os


def main() -> None:
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(), host=os.environ.get("OA_HOST", "0.0.0.0"), port=int(os.environ.get("OA_PORT", "8787")),
                proxy_headers=True, forwarded_allow_ips=os.environ.get("OA_TRUSTED_PROXIES", "127.0.0.1"))


if __name__ == "__main__":
    main()
