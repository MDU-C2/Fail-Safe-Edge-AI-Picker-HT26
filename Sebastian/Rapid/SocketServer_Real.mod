MODULE SocketServer
  VAR socketdev server;
  VAR socketdev client;
  VAR string msg;
  VAR num x;
  VAR num y;
  VAR num z;
  VAR robtarget pTarget;

  ! Safe zone limits in mm (wobj0, tool0 = flange). Tighten to tested points.
  CONST num X_MIN := 200;
  CONST num X_MAX := 450;
  CONST num Y_MIN := 50;
  CONST num Y_MAX := 350;
  CONST num Z_MIN := 250;
  CONST num Z_MAX := 350;

  PROC main()
    SocketClose server;
    SocketCreate server;
    SocketBind server, "192.168.125.1", 5000;
    SocketListen server;
    TPWrite "Waiting for coordinates...";
    ConfJ \Off;

    WHILE TRUE DO
      SocketAccept server, client \Time:=WAIT_MAX;
      SocketReceive client \Str:=msg \Time:=WAIT_MAX;
      TPWrite "Got: " + msg;

      IF NOT ParseXYZ(msg) THEN
        TPWrite "Rejected: bad format";
        SocketSend client \Str:="ERR bad format";
      ELSEIF NOT InSafeZone() THEN
        TPWrite "Rejected: outside safe zone";
        SocketSend client \Str:="ERR outside safe zone";
      ELSE
        pTarget := CRobT(\Tool:=tool0 \WObj:=wobj0);
        TPWrite "Now: " \Pos:=pTarget.trans;
        pTarget.trans := [x, y, z];
        MoveJ pTarget, v50, fine, tool0 \WObj:=wobj0;
        SocketSend client \Str:="DONE";
      ENDIF
      SocketClose client;
    ENDWHILE
  ENDPROC

  FUNC bool InSafeZone()
    RETURN x >= X_MIN AND x <= X_MAX AND y >= Y_MIN AND y <= Y_MAX AND z >= Z_MIN AND z <= Z_MAX;
  ENDFUNC

  FUNC bool ParseXYZ(string s)
    VAR num c1;
    VAR num c2;
    c1 := StrFind(s, 1, ",");
    IF c1 > StrLen(s) RETURN FALSE;
    c2 := StrFind(s, c1 + 1, ",");
    IF c2 > StrLen(s) RETURN FALSE;
    IF NOT StrToVal(StrPart(s, 1, c1 - 1), x) RETURN FALSE;
    IF NOT StrToVal(StrPart(s, c1 + 1, c2 - c1 - 1), y) RETURN FALSE;
    IF NOT StrToVal(StrPart(s, c2 + 1, StrLen(s) - c2), z) RETURN FALSE;
    RETURN TRUE;
  ENDFUNC
ENDMODULE