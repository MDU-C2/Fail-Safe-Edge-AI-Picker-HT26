MODULE LeftArm

  VAR robtarget pTarget;

  PROC main()
    ConfJ \Off;
    TPWrite "Left arm ready";

    WHILE TRUE DO
      WaitUntil move_request = TRUE;

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

      move_request := FALSE;
    ENDWHILE
  ENDPROC

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
