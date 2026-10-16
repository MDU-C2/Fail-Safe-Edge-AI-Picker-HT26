MODULE LegacyShared

  ! Variables shared between the CommServer task and the motion task.
  ! Load this module into both tasks; RAPID links PERS variables with
  ! the same name and type across tasks.

  PERS bool move_request := FALSE;
  PERS bool go_home := FALSE;
  PERS bool move_ok := FALSE;
  PERS bool use_rot := FALSE;
  PERS pos target_pos := [0, 0, 0];
  PERS orient target_rot := [1, 0, 0, 0];

ENDMODULE
