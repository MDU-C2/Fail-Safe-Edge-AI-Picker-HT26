MODULE LeftArm

  ! TCP z = 159: touch-off showed the gripper is ~23 mm longer than 136 (table now = z 0)
  PERS tooldata tGrip := [TRUE, [[0, 0, 159], [1, 0, 0, 0]], [0.230, [8.2, 11.7, 52.0], [1, 0, 0, 0], 0.00021, 0.00024, 0.00009]];

  CONST num MARGIN := 25;               ! used by TeachMaxHeight

  VAR speeddata vTravel := [100, 500, 5000, 1000];
  VAR speeddata vNear := [50, 500, 5000, 1000];

  ! Orientation is never forced here. Every motion command can carry rx,ry,rz
  ! (CommServer turns them into target_rot / use_rot). Without them the arm keeps
  ! the last orientation it was given (cur_rot).
  VAR robtarget pBase;                  ! robconf + arm angle (eax_a) used for new targets
  VAR orient cur_rot;                   ! orientation used when a command has no rx,ry,rz
  VAR robtarget pApp;
  VAR robtarget pPoint;
  VAR robtarget pTarget;
  VAR num x;
  VAR num y;
  VAR num z;
  VAR string reply;

  PROC main()
    ! Clear requests left over from an earlier run (PERS keeps its values)
    arm_ready := FALSE;
    move_request := FALSE;

    ConfJ \Off;
    ConfL \Off;
    IF NOT g_IsCalibrated() THEN
      g_Init \Calibrate;
    ENDIF
    g_GripOut;

    ! No motion at start. Keep whatever orientation the arm has right now.
    TakeCurrent;

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

      ! New orientation given? Use it for this and every following command.
      ! CHECK only tests, so it must not change cur_rot.
      IF use_rot AND arm_cmd <> "CHECK" AND arm_cmd <> "HOME" THEN
        cur_rot := target_rot;
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
        pPoint := MakeTarget(x, y, z);
        MoveL pPoint, vTravel, z10, tGrip \WObj:=wobj0;
        reply := "DONE";
      CASE "WAYEND":
        pPoint := MakeTarget(x, y, z);
        MoveL pPoint, vTravel, fine, tGrip \WObj:=wobj0;
        reply := "DONE";
      CASE "GOTO":
        ! Joint move: use this for big orientation changes (e.g. from HOME)
        pTarget := MakeTarget(x, y, z);
        IF CanReach(pTarget) THEN
          MoveJ pTarget, vTravel, fine, tGrip \WObj:=wobj0;
          TakeCurrent;
          reply := "DONE";
        ELSE
          TPWrite "Rejected: unreachable";
          reply := "ERR unreachable";
        ENDIF
      CASE "HOME":
        GoHomeLeft;
        TakeCurrent;
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

  ! Remember the arm's current pose: its orientation becomes cur_rot,
  ! its robconf and arm angle are used for the next targets.
  PROC TakeCurrent()
    pBase := CRobT(\Tool:=tGrip \WObj:=wobj0);
    cur_rot := pBase.rot;
  ENDPROC

  ! Target at x, y, z with the current orientation
  FUNC robtarget MakeTarget(num px, num py, num pz)
    VAR robtarget p;
    p := pBase;
    p.trans := [px, py, pz];
    p.rot := cur_rot;
    RETURN p;
  ENDFUNC

  ! Optional: jog to camera height (Tool tGrip), PP to Routine -> TeachMaxHeight -> Start
  PROC TeachMaxHeight()
    VAR robtarget p;
    p := CRobT(\Tool:=tGrip \WObj:=wobj0);
    zMax := p.trans.z - MARGIN;
    TPWrite "Ceiling saved: Z = " \Num:=zMax;
  ENDPROC

  ! ================= MOTION =================

  PROC SetTargets()
    pApp := MakeTarget(x, y, z + APPROACH);
    pPoint := MakeTarget(x, y, z);
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

  ! Reach check for x, y, z with the given orientation (or the current one). No motion.
  FUNC bool CanReachXYZ()
    VAR robtarget pTest;
    pTest := MakeTarget(x, y, z);
    IF use_rot THEN
      pTest.rot := target_rot;
    ENDIF
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