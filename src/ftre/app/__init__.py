"""Process applications and their Composition Roots."""
# 进程级应用包：收拢 CLI、Gateway 启动与 HTTP Host 三类进程边界。
# 这里只做"进程层"的事情（入口、装配、宿主、启停编排），
# 产品能力一律放在 services/features 中，由 Composition 组装后在此启动。
