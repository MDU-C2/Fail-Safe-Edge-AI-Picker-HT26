MODULE SharedVars

  PERS pos target_pos_left := [0, 0, 0];
  PERS orient target_rot_left := [1, 0, 0, 0];
  PERS bool use_rot_left := FALSE;
  PERS bool go_home_left := FALSE;
  PERS bool move_request_left := FALSE;
  PERS bool move_ok_left := FALSE;
  PERS string grip_cmd_left := "";            ! "" = nothing, "GRIP" = close, "OPEN" = open

  PERS pos target_pos_right := [0, 0, 0];
  PERS orient target_rot_right := [1, 0, 0, 0];
  PERS bool use_rot_right := FALSE;
  PERS bool go_home_right := FALSE;
  PERS bool move_request_right := FALSE;
  PERS bool move_ok_right := FALSE;
  PERS string grip_cmd_right := "";       
       
ENDMODULE