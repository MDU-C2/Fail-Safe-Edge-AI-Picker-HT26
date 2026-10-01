MODULE CommServer

  ! Non-motion task. Change SERVER_IP to "127.0.0.1" for simulation.
  CONST string SERVER_IP := "192.168.125.1";
  CONST num PORT := 5001;               ! must match DEFAULT_PORT in the Python scripts

  ! Allowed box for TCP targets (wobj0, tGrip, table = z 0). Keep in sync with BOUNDS in Python.
  CONST num X_MIN := 350;
  CONST num X_MAX := 480;
  CONST num Y_MIN := -250;
  CONST num Y_MAX := 350;
  CONST num Z_MIN := 5;

  CONST num MAX_SPEED := 1000;          ! travel speed limit
  CONST num MAX_NEAR := 300;            ! pick speed limit
  CONST num ARM_TIMEOUT := 90;          ! s to wait for the arm before giving up

  VAR socketdev server;
  VAR socketdev client;
  VAR string msg;
  VAR string cmd;
  VAR string reply;
  VAR num x;
  VAR num y;
  VAR num z;
  VAR num vals{6};
  VAR num nvals;
  VAR bool timed_out;

  PROC main()
    move_request := FALSE;
    SocketClose server;
    SocketCreate server;
    SocketBind server, SERVER_IP, PORT;
    SocketListen server;
    TPWrite "Comm server listening on port " \Num:=PORT;

    WHILE TRUE DO
      HandleClient;
    ENDWHILE
  ENDPROC

  PROC HandleClient()
    SocketAccept server, client \Time:=WAIT_MAX;
    SocketReceive client \Str:=msg \Time:=WAIT_MAX;
    IF StrMatch(msg, 1, "CHECK") <> 1 THEN
      TPWrite "Got: " + msg;
    ENDIF

    Dispatch;

    SocketSend client \Str:=reply;
    SocketClose client;
  ERROR
    IF ERRNO = ERR_SOCK_CLOSED OR ERRNO = ERR_SOCK_TIMEOUT THEN
      TPWrite "Client disconnected";
      SocketClose client;
      RETURN;
    ENDIF
  ENDPROC

  ! Decides what to do with the message and sets reply
  PROC Dispatch()
    IF NOT ParseMsg(msg) THEN
      reply := "ERR bad format";
    ELSEIF cmd = "LIMITS" THEN
      reply := "ZMAX," + NumToStr(zMax, 1);
    ELSEIF cmd = "HOME" THEN
      AskArm "HOME";
    ELSEIF nvals < 3 THEN
      reply := "ERR bad format";
    ELSEIF cmd = "SPEED" THEN
      ! x = travel speed, y = pick speed (0 = keep current)
      IF x < 10 OR x > MAX_SPEED OR (y <> 0 AND (y < 5 OR y > MAX_NEAR)) THEN
        reply := "ERR speed out of range";
      ELSE
        travel_speed := x;
        IF y > 0 THEN
          pick_speed := y;
        ENDIF
        TPWrite "Travel speed set to " \Num:=travel_speed;
        TPWrite "Pick speed set to " \Num:=pick_speed;
        reply := "DONE";
      ENDIF
    ELSEIF cmd = "CHECK" THEN
      ! No motion: the arm checks reach, this task checks the limits
      AskArm "CHECK";
      IF reply = "OK" THEN
        IF NOT InSafeZone() THEN
          reply := "BLOCKED";
        ENDIF
      ENDIF
    ELSEIF NOT InSafeZone() THEN
      TPWrite "Rejected: outside safe zone or above ceiling";
      reply := "ERR outside safe zone";
    ELSEIF (cmd = "PICK" OR cmd = "PLACE" OR cmd = "MOVE") AND z + APPROACH > zMax THEN
      TPWrite "Rejected: approach point would be above ceiling";
      reply := "ERR approach above max height";
    ELSEIF cmd = "PICK" OR cmd = "PLACE" OR cmd = "MOVE" OR cmd = "WAYPOINT"
        OR cmd = "WAYEND" OR cmd = "GOTO" THEN
      AskArm cmd;
    ELSE
      TPWrite "Rejected: unknown command";
      reply := "ERR unknown command";
    ENDIF
  ENDPROC

  ! Hand a command to the left arm and wait for its answer
  PROC AskArm(string c)
    IF NOT arm_ready THEN
      reply := "ERR arm not ready";
      RETURN;
    ENDIF
    arm_cmd := c;
    target_pos := [x, y, z];
    ! Any command can carry rx,ry,rz (degrees). Without them the arm keeps its orientation.
    use_rot := nvals = 6;
    IF use_rot THEN
      target_rot := OrientZYX(vals{6}, vals{5}, vals{4});
    ENDIF
    arm_reply := "";
    move_request := TRUE;

    WaitUntil move_request = FALSE \MaxTime:=ARM_TIMEOUT \TimeFlag:=timed_out \PollRate:=0.01;
    IF timed_out THEN
      move_request := FALSE;
      reply := "ERR arm timeout";
    ELSE
      reply := arm_reply;
    ENDIF
  ENDPROC

  FUNC bool InSafeZone()
    RETURN x >= X_MIN AND x <= X_MAX AND y >= Y_MIN AND y <= Y_MAX
       AND z >= Z_MIN AND z <= zMax;
  ENDFUNC

  ! Accepts:
  !   "CMD,x,y,z"            e.g. PICK,400,180,40   SPEED,250,100,0   (keeps orientation)
  !   "CMD,x,y,z,rx,ry,rz"   e.g. GOTO,415,200,250,180,0,0          (new orientation, degrees)
  !   "CMD"                  e.g. HOME   LIMITS
  !   "x,y,z"                plain point  -> GOTO
  !   "x,y,z,rx,ry,rz"       point + rotation in degrees -> GOTO
  FUNC bool ParseMsg(string s)
    VAR num start;
    VAR num c;
    VAR num dummy;
    VAR string tok;

    start := 1;
    nvals := 0;
    c := StrFind(s, start, ",");
    tok := StrPart(s, start, c - start);

    IF StrToVal(tok, dummy) THEN
      cmd := "GOTO";
    ELSE
      cmd := tok;
      IF c > StrLen(s) THEN
        RETURN TRUE;
      ENDIF
      start := c + 1;
    ENDIF

    WHILE nvals < 6 DO
      c := StrFind(s, start, ",");
      nvals := nvals + 1;
      IF NOT StrToVal(StrPart(s, start, c - start), vals{nvals}) THEN
        RETURN FALSE;
      ENDIF
      IF c > StrLen(s) THEN
        IF nvals >= 3 THEN
          x := vals{1};
          y := vals{2};
          z := vals{3};
        ENDIF
        RETURN nvals = 3 OR nvals = 6;
      ENDIF
      start := c + 1;
    ENDWHILE
    RETURN FALSE;
  ENDFUNC

ENDMODULE