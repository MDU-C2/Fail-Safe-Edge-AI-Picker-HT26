MODULE SocketServer
  VAR socketdev server;
  VAR socketdev client;
  VAR string msg;
  VAR num x;
  VAR num y;
  VAR num z;
  VAR robtarget pTarget;

  PROC main()
    SocketClose server;
    SocketCreate server;
    SocketBind server, "127.0.0.1", 5000;
    SocketListen server;
    TPWrite "Waiting for coordinates...";
    ConfJ \Off;

    WHILE TRUE DO
      SocketAccept server, client \Time:=WAIT_MAX;
      SocketReceive client \Str:=msg \Time:=WAIT_MAX;
      TPWrite "Got: " + msg;

      IF ParseXYZ(msg) THEN
        pTarget := CRobT(\Tool:=tool0 \WObj:=wobj0);
        TPWrite "Now: " \Pos:=pTarget.trans;
        pTarget.trans := [x, y, z];
        MoveJ pTarget, v100, fine, tool0 \WObj:=wobj0;
        SocketSend client \Str:="DONE";
      ELSE
        SocketSend client \Str:="ERR bad format";
      ENDIF
      SocketClose client;
    ENDWHILE
  ENDPROC

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