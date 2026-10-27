"""Entry point: python -m airbridge"""
import logging
import os

import uvicorn


def main():
    logging.basicConfig(level=os.environ.get("AIRBRIDGE_LOG", "INFO"),
                        format="%(asctime)s %(name)-6s %(levelname)-7s %(message)s")
    from .web import app, config
    uvicorn.run(app, host=config["web"]["host"], port=int(config["web"]["port"]),
                log_level="warning", ws_ping_interval=20)


if __name__ == "__main__":
    main()
