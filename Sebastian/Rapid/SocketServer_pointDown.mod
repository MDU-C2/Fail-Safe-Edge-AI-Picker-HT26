MODULE SocketServer
  ! Sim: "127.0.0.1"   Real YuMi (service port): "192.168.125.1"
  CONST string SERVER_IP := "192.168.125.1";
  CONST num PORT := 5001;

  ! TCP z: use your value from the ruler test. Load data from ABB spec (servo gripper + fingers).
  PERS tooldata tGrip := [TRUE, [[0, 0, 136], [1, 0, 0, 0]], [0.230, [8.2, 11.7, 52.0], [1, 0, 0, 0], 0.00021, 0.00024, 0.00009]];

  CONST num APPROACH := 100;

  ! Allowed box for TCP targets (wobj0). Keep in sync with BOUNDS in Python.
  CONST num X_MIN := 350;
  CONST num X_MAX := 480;
  CONST num Y_MIN := 50;
  CONST num Y_MAX := 350;
  CONST num Z_MIN := 20;

  ! ---------- CEILING (camera height) ----------
  CONST num MARGIN := 25;
  PERS num zMax := 423;

  ! ---------- START POSE ----------
  ! Gripper pointing straight down (measured on the pendant: q1~0 q2~1 q3~0 q4~0)
  CONST orient DOWN_ROT := [0, 1, 0, 0];
  ! Height the gripper goes up to before turning, so the fingers can't hit the table
  CONST num SAFE_Z := 200;

  VAR socketdev server;
  VAR socketdev client;
  VAR string msg;
  VAR string cmd;
  VAR num x;
  VAR num y;
  VAR num z;
  VAR robtarget pRef;
  VAR robtarget pApp;
  VAR robtarget pPoint;

  PROC main()
    ConfJ \Off;
    ConfL \Off;
    IF NOT g_IsCalibrated() THEN
      g_Init \Calibrate;
    ENDIF
    g_GripOut;

    ! Turn the gripper to point straight down and use that as the reference pose
    PointDown;
    TPWrite "Ceiling (max height) Z = " \Num:=zMax;

    SocketClose server;
    SocketCreate server;
    SocketBind server, SERVER_IP, PORT;
    SocketListen server;
    TPWrite "Ready. Waiting for commands...";

    WHILE TRUE DO
      SocketAccept server, client \Time:=WAIT_MAX;
      SocketReceive client \Str:=msg \Time:=WAIT_MAX;
      IF StrMatch(msg, 1, "CHECK") <> 1 THEN
        TPWrite "Got: " + msg;
      ENDIF

      IF NOT ParseCmd(msg) THEN
        TPWrite "Rejected: bad format";
        SocketSend client \Str:="ERR bad format";
      ELSEIF cmd = "CHECK" THEN
        ! No motion: is the point reachable, and is it allowed?
        IF NOT Reachable() THEN
          SocketSend client \Str:="NO_REACH";
        ELSEIF NOT InSafeZone() THEN
          SocketSend client \Str:="BLOCKED";
        ELSE
          SocketSend client \Str:="OK";
        ENDIF
      ELSEIF cmd = "LIMITS" THEN
        SocketSend client \Str:="ZMAX," + NumToStr(zMax, 1);
      ELSEIF NOT InSafeZone() THEN
        TPWrite "Rejected: outside safe zone or above ceiling";
        SocketSend client \Str:="ERR outside safe zone";
      ELSEIF (cmd = "PICK" OR cmd = "PLACE" OR cmd = "MOVE") AND z + APPROACH > zMax THEN
        TPWrite "Rejected: approach point would be above ceiling";
        SocketSend client \Str:="ERR approach above max height";
      ELSEIF cmd = "PICK" THEN
        DoPick;
        SocketSend client \Str:="DONE";
      ELSEIF cmd = "PLACE" THEN
        DoPlace;
        SocketSend client \Str:="DONE";
      ELSEIF cmd = "MOVE" THEN
        DoMove;
        SocketSend client \Str:="DONE";
      ELSEIF cmd = "WAYPOINT" THEN
        pPoint := pRef;
        pPoint.trans := [x, y, z];
        MoveL pPoint, v100, z10, tGrip \WObj:=wobj0;
        SocketSend client \Str:="DONE";
      ELSEIF cmd = "WAYEND" THEN
        pPoint := pRef;
        pPoint.trans := [x, y, z];
        MoveL pPoint, v100, fine, tGrip \WObj:=wobj0;
        SocketSend client \Str:="DONE";
      ELSE
        TPWrite "Rejected: unknown command";
        SocketSend client \Str:="ERR unknown command";
      ENDIF
      SocketClose client;
    ENDWHILE
  ENDPROC

  ! ================= START POSE =================

  ! 1. Go straight up to SAFE_Z if lower (same orientation, no turning near the table)
  ! 2. Check that pointing down is reachable here
  ! 3. Turn the gripper to point straight down, slowly
  PROC PointDown()
    VAR robtarget pNow;
    VAR robtarget pDown;

    pNow := CRobT(\Tool:=tGrip \WObj:=wobj0);
    pDown := pNow;

    IF pNow.trans.z < SAFE_Z THEN
      pDown.trans.z := SAFE_Z;
      TPWrite "Moving up to safe height before turning...";
      MoveL pDown, v50, fine, tGrip \WObj:=wobj0;
    ENDIF

    pDown.rot := DOWN_ROT;
    IF NOT CanReach(pDown) THEN
      TPWrite "Can't point the gripper down from here.";
      TPWrite "Jog the arm closer to the work area and press Play again.";
      Stop;
    ENDIF

    TPWrite "Turning gripper to point straight down...";
    MoveJ pDown, v50, fine, tGrip \WObj:=wobj0;
    pRef := CRobT(\Tool:=tGrip \WObj:=wobj0);
    TPWrite "Start pose set.";
  ENDPROC

  ! ================= TEACH (optional, PP to Routine, then Start) =================

  PROC TeachMaxHeight()
    VAR robtarget p;
    p := CRobT(\Tool:=tGrip \WObj:=wobj0);
    zMax := p.trans.z - MARGIN;
    TPWrite "Ceiling saved: Z = " \Num:=zMax;
  ENDPROC

  ! ================= MOTION =================

  PROC SetTargets()
    pApp := pRef;
    pApp.trans := [x, y, z + APPROACH];
    pPoint := pRef;
    pPoint.trans := [x, y, z];
  ENDPROC

  PROC DoPick()
    SetTargets;
    g_GripOut;
    MoveL pApp, v100, fine, tGrip \WObj:=wobj0;
    MoveL pPoint, v20, fine, tGrip \WObj:=wobj0;
    g_GripIn;
    MoveL pApp, v50, fine, tGrip \WObj:=wobj0;
  ENDPROC

  PROC DoPlace()
    SetTargets;
    MoveL pApp, v100, fine, tGrip \WObj:=wobj0;
    MoveL pPoint, v20, fine, tGrip \WObj:=wobj0;
    g_GripOut;
    MoveL pApp, v50, fine, tGrip \WObj:=wobj0;
  ENDPROC

  PROC DoMove()
    SetTargets;
    MoveL pApp, v100, fine, tGrip \WObj:=wobj0;
    MoveL pPoint, v20, fine, tGrip \WObj:=wobj0;
  ENDPROC

  ! ================= CHECKS =================

  FUNC bool InSafeZone()
    RETURN x >= X_MIN AND x <= X_MAX AND y >= Y_MIN AND y <= Y_MAX
       AND z >= Z_MIN AND z <= zMax;
  ENDFUNC

  FUNC bool Reachable()
    VAR robtarget pTest;
    pTest := pRef;
    pTest.trans := [x, y, z];
    RETURN CanReach(pTest);
  ENDFUNC

  ! Controller's own inverse kinematics. No motion.
  FUNC bool CanReach(robtarget p)
    VAR jointtarget jTest;
    jTest := CalcJointT(p, tGrip \WObj:=wobj0);
    RETURN TRUE;
  ERROR
    SkipWarn;
    RETURN FALSE;
  ENDFUNC

  FUNC bool ParseCmd(string s)
    VAR num c0;
    c0 := StrFind(s, 1, ",");
    IF c0 > StrLen(s) RETURN FALSE;
    cmd := StrPart(s, 1, c0 - 1);
    RETURN ParseXYZ(StrPart(s, c0 + 1, StrLen(s) - c0));
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