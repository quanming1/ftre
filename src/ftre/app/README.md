# app

`app` owns process boundaries only: CLI, Gateway bootstrap, FastAPI Host and
uvicorn.  It does not own Session, Agent, Tool or Feature business rules.

