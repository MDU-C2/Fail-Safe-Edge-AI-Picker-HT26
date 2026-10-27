MODULE RightArm

  VAR robtarget pTarget;
  VAR jointtarget jTarget;                     ! CHANGED: joint angles found by Reachable
  ! The cup the right arm holds: mass kg, centre of gravity mm from the flange (guess: mug ~0.35 kg,
  ! ~120 mm out). Without it the robot sees the heavy cup as a collision when turning it over.
  PERS loaddata cup_load := [0.35, [0, 0, 120], [1, 0, 0, 0], 0, 0, 0];

  PROC main()
    ! Clear requests left over from an earlier run (PERS keeps its values)
    move_request_right := FALSE;
    go_home_right := FALSE;
    use_rot_right := FALSE;
    grip_cmd_right := "";

    ConfJ \Off;
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
        g_GripIn;
        GripLoad cup_load;                     ! holding a cup now
        grip_cmd_right := "";
        move_ok_right := TRUE;
      ELSEIF grip_cmd_right = "OPEN" THEN
        g_GripOut;
        GripLoad load0;                        ! no cup any more
        grip_cmd_right := "";
        move_ok_right := TRUE;
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