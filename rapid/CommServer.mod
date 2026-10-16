MODULE CommServer

  ! Non-motion task. Change SERVER_IP to "127.0.0.1" for simulation.
  ! Left arm:  "x,y,z"  "x,y,z,rx,ry,rz"  "HOME"  "GRIP"  "OPEN"  "STATUS"
  ! Right arm: same with "R," in front, e.g. "R,400,-150,300"  "R,HOME"  "R,STATUS"
  ! A job answers "STARTED" at once. Then ask "STATUS": BUSY / DONE / ERR move failed
  CONST string SERVER_IP := "192.168.125.1";
  CONST num PORT := 5001;

  VAR socketdev server;
  VAR socketdev client;
  VAR string msg;
  VAR num x;
  VAR num y;
  VAR num z;
  VAR num vals{6};
  VAR num nvals;
  VAR bool right;                              ! TRUE = message is for the right arm

  ! Safe zone limits in mm (wobj0, tool0 = flange). Tighten to tested points.
  CONST num X_MIN := -1000;
  CONST num X_MAX := 1000;
  CONST num Y_MIN := -1000;
  CONST num Y_MAX := 1000;
  CONST num Z_MIN := -1000;
  CONST num Z_MAX := 1000;

  PROC main()
    move_request_left := FALSE;
    go_home_left := FALSE;
    move_request_right := FALSE;
    go_home_right := FALSE;
    SocketClose server;
    SocketCreate server;
    SocketBind server, SERVER_IP, PORT;
    SocketListen server;
    TPWrite "Waiting for coordinates...";

    WHILE TRUE DO
      HandleClient;
    ENDWHILE
  ENDPROC

  PROC HandleClient()
    SocketAccept server, client \Time:=WAIT_MAX;
    SocketReceive client \Str:=msg \Time:=WAIT_MAX;
    IF msg <> "STATUS" AND msg <> "R,STATUS" THEN   ! ASYNC: don't log the status polls
      TPWrite "Got: " + msg;
    ENDIF

    ! "R," in front = right arm. Cut it off, then read the rest exactly like before
    right := FALSE;
    IF StrLen(msg) > 2 THEN
      IF StrPart(msg, 1, 2) = "R," THEN
        right := TRUE;
        msg := StrPart(msg, 3, StrLen(msg) - 2);
      ENDIF
    ENDIF

    IF msg = "STATUS" THEN                     ! ASYNC: "is this arm finished?"
      SendStatus;
    ELSEIF ArmBusy() THEN                      ! ASYNC: arm still moving, refuse new job
      SocketSend client \Str:="ERR busy";
    ELSEIF msg = "HOME" THEN
      IF right THEN
        go_home_right := TRUE;
        RunMoveRight;
      ELSE
        go_home_left := TRUE;
        RunMoveLeft;
      ENDIF
    ELSEIF msg = "GRIP" OR msg = "OPEN" THEN
      IF right THEN
        grip_cmd_right := msg;
        go_home_right := FALSE;
        RunMoveRight;
      ELSE
        grip_cmd_left := msg;
        go_home_left := FALSE;
        RunMoveLeft;
      ENDIF
    ELSEIF NOT ParseMsg(msg) THEN
      TPWrite "Rejected: bad format";
      SocketSend client \Str:="ERR bad format";
    ELSEIF NOT InSafeZone() THEN
      TPWrite "Rejected: outside safe zone";
      SocketSend client \Str:="ERR outside safe zone";
    ELSEIF right THEN
      target_pos_right := [x, y, z];
      use_rot_right := nvals = 6;
      IF use_rot_right target_rot_right := OrientZYX(vals{6}, vals{5}, vals{4});
      go_home_right := FALSE;
      RunMoveRight;
    ELSE
      target_pos_left := [x, y, z];
      use_rot_left := nvals = 6;
      IF use_rot_left target_rot_left := OrientZYX(vals{6}, vals{5}, vals{4});
      go_home_left := FALSE;
      RunMoveLeft;
    ENDIF
    SocketClose client;
  ERROR
    IF ERRNO = ERR_SOCK_CLOSED OR ERRNO = ERR_SOCK_TIMEOUT THEN
      TPWrite "Client disconnected";
      SocketClose client;
      RETURN;
    ENDIF
  ENDPROC

  PROC RunMoveLeft()
    move_request_left := TRUE;                 ! start the job
    SocketSend client \Str:="STARTED";         ! ASYNC: answer at once, don't wait
  ENDPROC

  PROC RunMoveRight()
    move_request_right := TRUE;
    SocketSend client \Str:="STARTED";         ! ASYNC
  ENDPROC

  ! ASYNC: TRUE if the arm this message is for is still working
  FUNC bool ArmBusy()
    IF right RETURN move_request_right;
    RETURN move_request_left;
  ENDFUNC

  ! ASYNC: answer to "STATUS" / "R,STATUS"
  PROC SendStatus()
    VAR bool isBusy;
    VAR bool ok;
    IF right THEN
      isBusy := move_request_right;
      ok := move_ok_right;
    ELSE
      isBusy := move_request_left;
      ok := move_ok_left;
    ENDIF
    IF isBusy THEN
      SocketSend client \Str:="BUSY";
    ELSEIF ok THEN
      SocketSend client \Str:="DONE";
    ELSE
      SocketSend client \Str:="ERR move failed";
    ENDIF
  ENDPROC

  FUNC bool InSafeZone()
    RETURN x >= X_MIN AND x <= X_MAX AND y >= Y_MIN AND y <= Y_MAX AND z >= Z_MIN AND z <= Z_MAX;
  ENDFUNC

  ! Accepts "x,y,z" or "x,y,z,rx,ry,rz" (mm, degrees)
  FUNC bool ParseMsg(string s)
    VAR num start;
    VAR num c;
    start := 1;
    nvals := 0;
    WHILE nvals < 6 DO
      c := StrFind(s, start, ",");
      nvals := nvals + 1;
      IF NOT StrToVal(StrPart(s, start, c - start), vals{nvals}) RETURN FALSE;
      IF c > StrLen(s) THEN
        x := vals{1};
        y := vals{2};
        z := vals{3};
        RETURN nvals = 3 OR nvals = 6;
      ENDIF
      start := c + 1;
    ENDWHILE
    RETURN FALSE;
  ENDFUNC

ENDMODULE