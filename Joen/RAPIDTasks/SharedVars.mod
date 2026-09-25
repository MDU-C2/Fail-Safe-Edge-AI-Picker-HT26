MODULE SharedVars

  ! Loaded into both T_ROB_L and the communication task.
  ! The communication task writes the target, then sets move_request.
  ! The left arm clears move_request when done and reports move_ok.
  PERS pos target_pos := [0, 0, 0];
  PERS orient target_rot := [1, 0, 0, 0];
  PERS bool use_rot := FALSE;
  PERS bool go_home := FALSE;
  PERS bool move_request := FALSE;
  PERS bool move_ok := FALSE;

ENDMODULE