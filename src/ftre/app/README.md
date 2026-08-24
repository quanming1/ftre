<!-- 中文说明：app 层只负责进程边界、CLI、Gateway bootstrap 和 HTTP Host；Session、Agent、Tool 等状态不能放在这里。 -->

# app

`app` owns process boundaries only: CLI, Gateway bootstrap, FastAPI Host and
uvicorn.  It does not own Session, Agent, Tool or Feature business rules.
