MODULE LegacyShared

  ! Command interface of the motion task. The gateway writes the target
  ! over Robot Web Services and then sets move_request; the motion task
  ! clears it when done and reports in move_ok / move_error.
  ! Load this module into both tasks; RAPID links PERS variables with
  ! the same name and type across tasks.

  PERS bool move_request := FALSE;
  PERS bool go_home := FALSE;
  PERS bool move_ok := FALSE;
  PERS string move_error := "";
  PERS bool use_rot := FALSE;
  PERS pos target_pos := [0, 0, 0];
  PERS orient target_rot := [1, 0, 0, 0];

ENDMODULE
