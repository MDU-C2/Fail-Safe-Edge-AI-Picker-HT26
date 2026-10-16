MODULE LegacyMotion

  ! Motion side of CommServer: waits for move_request, moves the arm
  ! (tool0 in wobj0) and reports the result in move_ok.

  ! YuMi calibration (sync) position, used for HOME.
  CONST jointtarget HOME_POS := [[0, -130, 30, 0, 40, 0], [135, 9E+09, 9E+09, 9E+09, 9E+09, 9E+09]];

  PROC main()
    move_request := FALSE;

    WHILE TRUE DO
      WaitUntil move_request;
      DoMove;
      move_request := FALSE;
    ENDWHILE
  ENDPROC

  PROC DoMove()
    VAR robtarget target;
    VAR jointtarget joints;

    move_ok := FALSE;

    IF go_home THEN
      MoveAbsJ HOME_POS, v200, fine, tool0;
    ELSE
      ! Keep the current orientation and arm angle unless a rotation was given.
      target := CRobT(\Tool:=tool0 \WObj:=wobj0);
      target.trans := target_pos;
      IF use_rot target.rot := target_rot;

      ! Raises ERR_ROBLIMIT or ERR_OUTSIDE_REACH if the pose cannot be reached.
      joints := CalcJointT(target, tool0 \WObj:=wobj0);
      MoveAbsJ joints, v200, fine, tool0;
    ENDIF

    move_ok := TRUE;

  ERROR
    IF ERRNO = ERR_ROBLIMIT OR ERRNO = ERR_OUTSIDE_REACH THEN
      SkipWarn;
      TPWrite "Target cannot be reached";
      RETURN;
    ENDIF
  ENDPROC

ENDMODULE
