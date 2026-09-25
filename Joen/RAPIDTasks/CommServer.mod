MODULE CommServer

  ! Non-motion task. Change SERVER_IP to "127.0.0.1" for simulation.
  CONST string SERVER_IP := "192.168.125.1";
  CONST num PORT := 5000;

  VAR socketdev server;
  VAR socketdev client;
  VAR string msg;
  VAR num x;
  VAR num y;
  VAR num z;
  VAR num vals{6};
  VAR num nvals;

  ! Safe zone limits in mm (wobj0, tool0 = flange). Tighten to tested points.
  CONST num X_MIN := 200;
  CONST num X_MAX := 450;
  CONST num Y_MIN := 50;
  CONST num Y_MAX := 350;
  CONST num Z_MIN := 250;
  CONST num Z_MAX := 350;

  PROC main()
    move_request := FALSE;
    go_home := FALSE;
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
    TPWrite "Got: " + msg;

    IF msg = "HOME" THEN
      go_home := TRUE;
      RunMove;
    ELSEIF NOT ParseMsg(msg) THEN
      TPWrite "Rejected: bad format";
      SocketSend client \Str:="ERR bad format";
    ELSEIF NOT InSafeZone() THEN
      TPWrite "Rejected: outside safe zone";
      SocketSend client \Str:="ERR outside safe zone";
    ELSE
      target_pos := [x, y, z];
      use_rot := nvals = 6;
      IF use_rot target_rot := OrientZYX(vals{6}, vals{5}, vals{4});
      go_home := FALSE;
      RunMove;
    ENDIF
    SocketClose client;
  ERROR
    IF ERRNO = ERR_SOCK_CLOSED OR ERRNO = ERR_SOCK_TIMEOUT THEN
      TPWrite "Client disconnected";
      SocketClose client;
      RETURN;
    ENDIF
  ENDPROC

  PROC RunMove()
    move_request := TRUE;
    WaitUntil move_request = FALSE;
    IF move_ok THEN
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