MODULE GripperStub

  ! SIMULATION ONLY. Load next to the arm modules on a virtual
  ! controller created without the SmartGripper option, where the
  ! YuMi gripper routines the arm modules call do not exist and
  ! RAPID refuses to start. Never load it on the real YuMi: it has
  ! the real routines in its YuMiLib system modules.
  !
  ! Only the calls made by LeftArmInstructions.mod and
  ! RightArmInstructions.mod, with the SmartGripper's parameters;
  ! the grippers do not move. stub_pos (mm) and stub_force (N) keep
  ! what the last call asked for, so a test can read them over RWS.

  VAR num stub_pos := 25;
  VAR num stub_force := 20;

  FUNC bool g_IsCalibrated()
    RETURN TRUE;
  ENDFUNC

  PROC g_Init(\num maxSpd, \num holdForce, \num phyLimit, \switch Calibrate, \switch Grip, \switch Outward, \switch Jog, \num phyLimitMin)
  ENDPROC

  PROC g_GripIn(\num holdForce, \num targetPos, \num posAllowance, \switch NoWait, \switch NotLimitDriveForce)
    stub_force := 20;
    IF Present(holdForce) stub_force := holdForce;
    stub_pos := 0;
    WaitTime 0.2;
  ENDPROC

  PROC g_GripOut(\num holdForce, \num targetPos, \num posAllowance, \switch NoWait, \switch NotLimitDriveForce)
    stub_pos := 25;
    WaitTime 0.2;
  ENDPROC

  PROC g_MoveTo(num targetPos, \switch NoWait)
    stub_pos := targetPos;
    WaitTime 0.2;
  ENDPROC

  FUNC num g_GetPos()
    RETURN stub_pos;
  ENDFUNC

ENDMODULE
