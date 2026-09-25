MODULE LeftArm

  VAR robtarget pTarget;

  PROC main()
    ! Clear requests left over from an earlier run (PERS keeps its values)
    move_request := FALSE;
    go_home := FALSE;
    use_rot := FALSE;

    ConfJ \Off;
    TPWrite "Left arm ready";

    WHILE TRUE DO
      WaitUntil move_request = TRUE;

      IF go_home THEN
        GoHomeLeft;
        move_ok := AtHome();
      ELSE
        pTarget := CRobT(\Tool:=tool0 \WObj:=wobj0);
        TPWrite "Now: " \Pos:=pTarget.trans;
        pTarget.trans := target_pos;
        IF use_rot pTarget.rot := target_rot;

        IF Reachable(pTarget) THEN
          MoveJ pTarget, v50, fine, tool0 \WObj:=wobj0;
          move_ok := TRUE;
        ELSE
          TPWrite "Rejected: unreachable";
          move_ok := FALSE;
        ENDIF
      ENDIF

      move_request := FALSE;
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
  FUNC bool Reachable(robtarget t)
    VAR jointtarget jt;
    jt := CalcJointT(t, tool0 \WObj:=wobj0);
    RETURN TRUE;
  ERROR
    IF ERRNO = ERR_ROBLIMIT OR ERRNO = ERR_OUTSIDE_REACH THEN
      RETURN FALSE;
    ENDIF
  ENDFUNC

ENDMODULE