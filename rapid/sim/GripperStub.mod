MODULE GripperStub

  ! SIMULATION ONLY. Load next to the arm modules on a virtual
  ! controller created without the SmartGripper option, where the
  ! YuMi gripper routines the arm modules call do not exist and
  ! RAPID refuses to start. Never load it on the real YuMi: it has
  ! the real routines in its YuMiLib system modules.
  !
  ! Only the calls made by LeftArmInstructions.mod and
  ! RightArmInstructions.mod; the grippers do not move.

  FUNC bool g_IsCalibrated()
    RETURN TRUE;
  ENDFUNC

  PROC g_Init(\switch Calibrate)
  ENDPROC

  PROC g_GripIn()
    WaitTime 0.2;
  ENDPROC

  PROC g_GripOut()
    WaitTime 0.2;
  ENDPROC

ENDMODULE
