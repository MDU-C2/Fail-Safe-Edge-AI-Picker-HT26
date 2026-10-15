MODULE SharedVars

  ! Loaded into both T_ROB_L and the communication task.
  ! PERS with the same name are shared between tasks.
  !
  ! Handshake:
  !   CommServer writes arm_cmd + target, then sets move_request := TRUE
  !   LeftArm does the work, writes arm_reply, then sets move_request := FALSE

  CONST num APPROACH := 100;            ! height above pick/place point (used by both tasks)

  PERS string arm_cmd := "";            ! PICK, PLACE, MOVE, WAYPOINT, WAYEND, GOTO, CHECK, HOME
  PERS string arm_reply := "";          ! DONE, OK, NO_REACH or ERR ...
  PERS pos target_pos := [0, 0, 0];
  PERS orient target_rot := [1, 0, 0, 0];
  PERS bool use_rot := FALSE;
  PERS bool move_request := FALSE;
  PERS bool arm_ready := FALSE;         ! TRUE once the arm has set its start pose

  PERS num travel_speed := 100;         ! mm/s between spots   (set with SPEED)
  PERS num pick_speed := 50;            ! mm/s near the object (set with SPEED)
  PERS num zMax := 400;                 ! ceiling (camera height)

ENDMODULE