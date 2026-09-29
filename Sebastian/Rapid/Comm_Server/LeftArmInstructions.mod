MODULE LeftArm

  ! TCP z = 159: touch-off showed the gripper is ~23 mm longer than 136 (table now = z 0)
  PERS tooldata tGrip := [TRUE, [[0, 0, 159], [1, 0, 0, 0]], [0.230, [8.2, 11.7, 52.0], [1, 0, 0, 0], 0.00021, 0.00024, 0.00009]];

  ! Gripper pointing straight down (measured on the pendant: q1~0 q2~1 q3~0 q4~0)
  CONST orient DOWN_ROT := [0, 1, 0, 0];
  CONST num SAFE_Z := 200;              ! go up to this before turning the gripper
  CONST num MARGIN := 25;               ! used by TeachMaxHeight

  VAR speeddata vTravel := [100, 500, 5000, 1000];
  VAR speeddata vNear := [50, 500, 5000, 1000];
  VAR robtarget pRef;
  VAR robtarget pApp;
  VAR robtarget pPoint;
  VAR robtarget pTarget;
  VAR num x;
  VAR num y;
  VAR num z;
  VAR string reply;
  VAR bool at_home := FALSE;

  PROC main()
    ! Clear requests left over from an earlier run (PERS keeps its values)
    arm_ready := FALSE;
    move_request := FALSE;
    at_home := FALSE;

    ConfJ \Off;
    ConfL \Off;
    IF NOT g_IsCalibrated() THEN
      g_Init \Calibrate;
    ENDIF
    g_GripOut;

    PointDown;
    TPWrite "Ceiling (max height) Z = " \Num:=zMax;
    arm_ready := TRUE;
    TPWrite "Left arm ready";

    WHILE TRUE DO
      WaitUntil move_request = TRUE \PollRate:=0.01;

      x := target_pos.x;
      y := target_pos.y;
      z := target_pos.z;
      vTravel := [travel_speed, 500, 5000, 1000];
      vNear := [pick_speed, 500, 5000, 1000];

      ! After HOME, go back to the known-good start pose before any other move
      IF at_home AND arm_cmd <> "HOME" AND arm_cmd <> "CHECK" THEN
        MoveJ pRef, vTravel, fine, tGrip \WObj:=wobj0;
        at_home := FALSE;
      ENDIF

      TEST arm_cmd
      CASE "CHECK":
        IF CanReachXYZ() THEN
          reply := "OK";
        ELSE
          reply := "NO_REACH";
        ENDIF
      CASE "PICK":
        DoPick;
        reply := "DONE";
      CASE "PLACE":
        DoPlace;
        reply := "DONE";
      CASE "MOVE":
        DoMove;
        reply := "DONE";
      CASE "WAYPOINT":
        pPoint := pRef;
        pPoint.trans := [x, y, z];
        MoveL pPoint, vTravel, z10, tGrip \WObj:=wobj0;
        reply := "DONE";
      CASE "WAYEND":
        pPoint := pRef;
        pPoint.trans := [x, y, z];
        MoveL pPoint, vTravel, fine, tGrip \WObj:=wobj0;
        reply := "DONE";
      CASE "GOTO":
        ! Plain point (optionally with rotation), from the friend's protocol
        pTarget := pRef;
        pTarget.trans := [x, y, z];
        IF use_rot THEN
          pTarget.rot := target_rot;
        ENDIF
        IF CanReach(pTarget) THEN
          MoveJ pTarget, vTravel, fine, tGrip \WObj:=wobj0;
          reply := "DONE";
        ELSE
          TPWrite "Rejected: unreachable";
          reply := "ERR unreachable";
        ENDIF
      CASE "HOME":
        GoHomeLeft;
        at_home := TRUE;
        IF AtHome() THEN
          reply := "DONE";
        ELSE
          reply := "ERR home blocked";
        ENDIF
      DEFAULT:
        reply := "ERR unknown arm command";
      ENDTEST

      arm_reply := reply;
      move_request := FALSE;
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

  ! Optional: jog to camera height (Tool tGrip), PP to Routine -> TeachMaxHeight -> Start
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

  ! Travel at vTravel, down/up near the object at vNear
  PROC DoPick()
    SetTargets;
    g_GripOut;
    MoveL pApp, vTravel, fine, tGrip \WObj:=wobj0;
    MoveL pPoint, vNear, fine, tGrip \WObj:=wobj0;
    g_GripIn;
    MoveL pApp, vNear, fine, tGrip \WObj:=wobj0;
  ENDPROC

  PROC DoPlace()
    SetTargets;
    MoveL pApp, vTravel, fine, tGrip \WObj:=wobj0;
    MoveL pPoint, vNear, fine, tGrip \WObj:=wobj0;
    g_GripOut;
    MoveL pApp, vNear, fine, tGrip \WObj:=wobj0;
  ENDPROC

  PROC DoMove()
    SetTargets;
    MoveL pApp, vTravel, fine, tGrip \WObj:=wobj0;
    MoveL pPoint, vNear, fine, tGrip \WObj:=wobj0;
  ENDPROC

  ! ================= CHECKS =================

  ! GoHomeLeft can stop early if blocked, so check where the arm ended up.
  FUNC bool AtHome()
    VAR jointtarget jt;
    jt := CJointT();
    RETURN Abs(jt.robax.rax_1 - home_left.robax.rax_1) < 1
       AND Abs(jt.robax.rax_2 - home_left.robax.rax_2) < 1
       AND Abs(jt.robax.rax_3 - home_left.robax.rax_3) < 1
       AND Abs(jt.robax.rax_4 - home_left.robax.rax_4) < 1
       AND Abs(jt.robax.rax_5 - home_left.robax.rax_5) < 1
       AND Abs(jt.robax.rax_6 - home_left.robax.rax_6) < 1
       AND Abs(jt.extax.eax_a - home_left.extax.eax_a) < 1;
  ENDFUNC

  ! Reach check for x, y, z with the start-pose orientation. No motion.
  FUNC bool CanReachXYZ()
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

ENDMODULE