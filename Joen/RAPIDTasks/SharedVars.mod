MODULE SharedVars

  ! Loaded into both T_ROB_L and the communication task.
  ! The communication task writes target_pos, then sets move_request.
  ! The left arm clears move_request when done and reports move_ok.
  PERS pos target_pos := [0, 0, 0];
  PERS bool move_request := FALSE;
  PERS bool move_ok := FALSE;
  PERS orient target_rot := [1, 0, 0, 0];
  PERS bool use_rot := FALSE;
ENDMODULE
