MODULE RightArm

  VAR robtarget pTarget;
  VAR jointtarget jTarget;                     ! CHANGED: joint angles found by Reachable

  PROC main()
    ! Clear requests left over from an earlier run (PERS keeps its values)
    move_request_right := FALSE;
    go_home_right := FALSE;
    use_rot_right := FALSE;
    grip_cmd_right := "";
    path_len_right := 0;

    ConfJ \Off;
    ConfL \Off;                                 ! path MoveL: take the configuration closest to the current one
    IF NOT g_IsCalibrated() THEN
      g_Init \Calibrate;
    ENDIF
    TPWrite "Right arm ready";

    WHILE TRUE DO
      WaitUntil move_request_right = TRUE;
      AccSet move_acc_right, move_ramp_right;

      IF go_home_right THEN
        GoHomeRight;
        move_ok_right := AtHome();
      ELSEIF grip_cmd_right = "GRIP" THEN
        g_GripIn \holdForce:=grip_force_right;
        grip_cmd_right := "";
        move_ok_right := TRUE;
      ELSEIF grip_cmd_right = "OPEN" THEN
        g_GripOut;
        grip_cmd_right := "";
        move_ok_right := TRUE;
      ELSEIF grip_cmd_right = "MOVE" THEN
        g_MoveTo grip_pos_right;
        grip_cmd_right := "";
        move_ok_right := TRUE;
      ELSEIF path_len_right > 0 THEN
        RunPath;
        path_len_right := 0;
      ELSE
        pTarget := CRobT(\Tool:=tool0 \WObj:=wobj0);
        TPWrite "Now: " \Pos:=pTarget.trans;
        pTarget.trans := target_pos_right;
        IF use_rot_right pTarget.rot := target_rot_right;

        IF Reachable(pTarget) THEN
          MoveAbsJ jTarget \NoEOffs, move_speed_right, fine, tool0;   ! CHANGED: exactly the joints the check found
          move_ok_right := TRUE;
        ELSE
          TPWrite "Rejected: unreachable";
          move_ok_right := FALSE;
        ENDIF
      ENDIF

      move_request_right := FALSE;
    ENDWHILE
  ENDPROC

  ! Path job. Checks every point first, so the arm never stops half way
  ! because of an unreachable point, then runs them all in one go: blended
  ! (zone) through the middle points, fine at the last one. Straight-line
  ! points use MoveL, the others the joint angles the check found.
  PROC RunPath()
    VAR robtarget p;
    VAR jointtarget jPath{8};
    VAR speeddata sp;
    VAR zonedata zn;
    VAR num n;
    VAR num r;

    move_ok_right := FALSE;
    n := path_len_right;
    IF n < 1 OR n > Dim(path_pos_right, 1) THEN
      TPWrite "Path rejected: bad length";
      RETURN;
    ENDIF

    p := CRobT(\Tool:=tool0 \WObj:=wobj0);   ! keeps the current arm angle and configuration
    FOR i FROM 1 TO n DO
      p.trans := path_pos_right{i};
      p.rot := path_rot_right{i};
      IF NOT Reachable(p) THEN
        TPWrite "Path rejected: unreachable point " \Num:=i;
        RETURN;
      ENDIF
      jPath{i} := jTarget;
      ! The next point starts from this point's configuration, as separate
      ! moves would (a cup turned over needs another wrist configuration).
      p := CalcRobT(jPath{i}, tool0 \WObj:=wobj0);
    ENDFOR

    IF path_acc_right > 0 THEN
      PathAccLim TRUE \AccMax:=path_acc_right, TRUE \DecelMax:=path_acc_right;
    ENDIF

    ! No waits or TPWrite in this loop, so the program runs ahead of the
    ! motion and the controller can blend each corner.
    FOR i FROM 1 TO n DO
      sp := move_speed_right;
      sp.v_tcp := path_speed_right{i};
      r := path_zone_right{i};
      IF i = n OR r <= 0 THEN
        zn := fine;
      ELSE
        zn := [FALSE, r, 1.5 * r, 1.5 * r, 0.15 * r, 1.5 * r, 0.15 * r];   ! same shape as z10, z20 ...
      ENDIF
      IF path_lin_right{i} THEN
        p := CalcRobT(jPath{i}, tool0 \WObj:=wobj0);   ! the checked point, with its configuration
        MoveL p, sp, zn, tool0 \WObj:=wobj0;
      ELSE
        MoveAbsJ jPath{i} \NoEOffs, sp, zn, tool0;
      ENDIF
    ENDFOR

    PathAccLim FALSE, FALSE;
    move_ok_right := TRUE;
  ENDPROC

  ! GoHomeRight can stop early if blocked, so check where the arm ended up.
  FUNC bool AtHome()
    VAR jointtarget jt;
    jt := CJointT();
    RETURN Abs(jt.robax.rax_1 - home_right.robax.rax_1) < 1
       AND Abs(jt.robax.rax_2 - home_right.robax.rax_2) < 1
       AND Abs(jt.robax.rax_3 - home_right.robax.rax_3) < 1
       AND Abs(jt.robax.rax_4 - home_right.robax.rax_4) < 1
       AND Abs(jt.robax.rax_5 - home_right.robax.rax_5) < 1
       AND Abs(jt.robax.rax_6 - home_right.robax.rax_6) < 1
       AND Abs(jt.extax.eax_a - home_right.extax.eax_a) < 1;
  ENDFUNC

  ! CalcJointT raises an error if the target can't be reached.
  ! CHANGED: keeps the joint angles it found in jTarget, so the move uses exactly those
  FUNC bool Reachable(robtarget t)
    jTarget := CalcJointT(t, tool0 \WObj:=wobj0);
    RETURN TRUE;
  ERROR
    IF ERRNO = ERR_ROBLIMIT OR ERRNO = ERR_OUTSIDE_REACH THEN
      RETURN FALSE;
    ENDIF
  ENDFUNC

ENDMODULE