MODULE LeftArm

  ! Gripper tool, kept so the YuMi background task finds tGrip (not used for moves)
  PERS tooldata tGrip := [TRUE, [[0, 0, 159], [1, 0, 0, 0]], [0.230, [8.2, 11.7, 52.0], [1, 0, 0, 0], 0.00021, 0.00024, 0.00009]];

  VAR robtarget pTarget;
  VAR jointtarget jTarget;                     ! CHANGED: joint angles found by Reachable

  PROC main()
    ! Clear requests left over from an earlier run (PERS keeps its values)
    move_request_left := FALSE;
    go_home_left := FALSE;
    use_rot_left := FALSE;
    grip_cmd_left := "";

    ConfJ \Off;
    IF NOT g_IsCalibrated() THEN
      g_Init \Calibrate;
    ENDIF
    TPWrite "Left arm ready";

    WHILE TRUE DO
      WaitUntil move_request_left = TRUE;
      AccSet move_acc_left, move_ramp_left;

      IF go_home_left THEN
        GoHomeLeft;
        move_ok_left := AtHome();
      ELSEIF grip_cmd_left = "GRIP" THEN
        g_GripIn;
        grip_cmd_left := "";
        move_ok_left := TRUE;
      ELSEIF grip_cmd_left = "OPEN" THEN
        g_GripOut;
        grip_cmd_left := "";
        move_ok_left := TRUE;
      ELSE
        pTarget := CRobT(\Tool:=tool0 \WObj:=wobj0);
        TPWrite "Now: " \Pos:=pTarget.trans;
        pTarget.trans := target_pos_left;
        IF use_rot_left pTarget.rot := target_rot_left;

        IF Reachable(pTarget) THEN
          MoveAbsJ jTarget \NoEOffs, move_speed_left, fine, tool0;   ! CHANGED: exactly the joints the check found
          move_ok_left := TRUE;
        ELSE
          TPWrite "Rejected: unreachable";
          move_ok_left := FALSE;
        ENDIF
      ENDIF

      move_request_left := FALSE;
    ENDWHILE
  ENDPROC

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