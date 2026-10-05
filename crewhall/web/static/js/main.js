"use strict";
render(); layout(); setConn(false); $("conn").textContent = "connecting…"; $("conn").className = "conn";
connect();
refreshUpdateStatus();
maybeOpenOnboarding();
