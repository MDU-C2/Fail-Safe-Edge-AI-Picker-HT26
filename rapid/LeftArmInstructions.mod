MODULE LeftArm

  ! Gripper tool, kept so the YuMi background task finds tGrip (not used for moves)
  PERS tooldata tGrip := [TRUE, [[0, 0, 155.5], [1, 0, 0, 0]], [0.230, [8.2, 11.7, 52.0], [1, 0, 0, 0], 0.00021, 0.00024, 0.00009]];

  VAR robtarget pTarget;
  VAR jointtarget jTarget;                     ! CHANGED: joint angles found by Reachable

  ! Gateway watchdog (see SharedVars): the last heartbeat and session
  ! seen, the time since the heartbeat last changed, whether it changed
  ! at all since RAPID started, whether its loss was handled, and
  ! whether the current job is the arm's own auto-home.
  VAR num hbLast;
  VAR num hbSession;
  VAR clock hbClock;
  VAR bool hbSeen;
  VAR bool hbLost;
  VAR bool autoJob;

  PROC main()
    ! Clear requests left over from an earlier run (PERS keeps its values)
    move_request_left := FALSE;
    go_home_left := FALSE;
    use_rot_left := FALSE;
    grip_cmd_left := "";
    path_len_left := 0;
    hbLast := gw_heartbeat;
    hbSession := gw_session;
    hbSeen := FALSE;
    hbLost := FALSE;
    autoJob := FALSE;
    ClkReset hbClock;
    ClkStart hbClock;

    ConfJ \Off;
    ConfL \Off;                                 ! path MoveL: take the configuration closest to the current one
    IF NOT g_IsCalibrated() THEN
      g_Init \Calibrate;
    ENDIF
    TPWrite "Left arm ready";

    WHILE TRUE DO
      WHILE NOT move_request_left DO           ! idle: watch the gateway meanwhile
        WaitTime 0.1;
        Watchdog;
      ENDWHILE
      IF autoJob THEN
        autoJob := FALSE;
      ELSE
        auto_home_left := FALSE;              ! a job from outside: the arm is no longer where it went on its own
      ENDIF
      AccSet move_acc_left, move_ramp_left;

      IF go_home_left THEN
        GoHomeLeft;
        move_ok_left := AtHome();
      ELSEIF grip_cmd_left = "GRIP" THEN
        g_GripIn \holdForce:=grip_force_left;
        holding_left := TRUE;
        grip_cmd_left := "";
        move_ok_left := TRUE;
      ELSEIF grip_cmd_left = "OPEN" THEN
        g_GripOut;
        holding_left := FALSE;
        grip_cmd_left := "";
        move_ok_left := TRUE;
      ELSEIF grip_cmd_left = "MOVE" THEN
        g_MoveTo grip_pos_left;
        holding_left := FALSE;
        grip_cmd_left := "";
        move_ok_left := TRUE;
      ELSEIF path_len_left > 0 THEN
        RunPath;
        path_len_left := 0;
      ELSE
        pTarget := CRobT(\Tool:=tool0 \WObj:=wobj0);
        TPWrite "Now: " \Pos:=pTarget.trans;
        pTarget.trans := target_pos_left;
        IF use_rot_left pTarget.rot := target_rot_left;

        IF Reachable(pTarget) THEN
          MoveAbsJ jTarget \NoEOffs, move_speed_left, fine, tool0;   ! CHANGED: exactly the joints the check found
          move_ok_left := TRUE;
        ELSE
          TPWrite "Rejected: unreachable";
          move_ok_left := FALSE;
        ENDIF
      ENDIF

      move_request_left := FALSE;
    ENDWHILE
  ENDPROC

  ! Path job. Checks every point first, so the arm never stops half way
  ! because of an unreachable point, then runs them all in one go: blended
  ! (zone) through the middle points, fine at the last one. Straight-line
  ! points use MoveL, the others the joint angles the check found.
  PROC RunPath()
    VAR robtarget p;
    VAR jointtarget jPath{8};
    VAR speeddata sp;
    VAR zonedata zn;
    VAR num n;
    VAR num r;

    move_ok_left := FALSE;
    n := path_len_left;
    IF n < 1 OR n > Dim(path_pos_left, 1) THEN
      TPWrite "Path rejected: bad length";
      RETURN;
    ENDIF

    p := CRobT(\Tool:=tool0 \WObj:=wobj0);   ! keeps the current arm angle and configuration
    FOR i FROM 1 TO n DO
      p.trans := path_pos_left{i};
      p.rot := path_rot_left{i};
      IF NOT Reachable(p) THEN
        TPWrite "Path rejected: unreachable point " \Num:=i;
        RETURN;
      ENDIF
      jPath{i} := jTarget;
      ! The next point starts from this point's configuration, as separate
      ! moves would (a cup turned over needs another wrist configuration).
      p := CalcRobT(jPath{i}, tool0 \WObj:=wobj0);
    ENDFOR

    IF path_acc_left > 0 THEN
      PathAccLim TRUE \AccMax:=path_acc_left, TRUE \DecelMax:=path_acc_left;
    ENDIF

    ! No waits or TPWrite in this loop, so the program runs ahead of the
    ! motion and the controller can blend each corner.
    FOR i FROM 1 TO n DO
      sp := move_speed_left;
      sp.v_tcp := path_speed_left{i};
      r := path_zone_left{i};
      IF i = n OR r <= 0 THEN
        zn := fine;
      ELSE
        zn := [FALSE, r, 1.5 * r, 1.5 * r, 0.15 * r, 1.5 * r, 0.15 * r];   ! same shape as z10, z20 ...
      ENDIF
      IF path_lin_left{i} THEN
        p := CalcRobT(jPath{i}, tool0 \WObj:=wobj0);   ! the checked point, with its configuration
        MoveL p, sp, zn, tool0 \WObj:=wobj0;
      ELSE
        MoveAbsJ jPath{i} \NoEOffs, sp, zn, tool0;
      ENDIF
    ENDFOR

    PathAccLim FALSE, FALSE;
    move_ok_left := TRUE;
  ENDPROC

  ! Gateway watchdog, called every 0.1 s while the arm is idle. A new
  ! session means the gateway restarted; a heartbeat unchanged for
  ! gw_timeout seconds means it is gone. Either way the arm goes home.
  PROC Watchdog()
    IF gw_session <> hbSession THEN
      hbSession := gw_session;
      Beat;
      AutoHome "Gateway restarted";
    ELSEIF gw_heartbeat <> hbLast THEN
      Beat;
    ELSEIF hbSeen AND hbLost = FALSE AND gw_timeout > 0 AND ClkRead(hbClock) > gw_timeout THEN
      hbLost := TRUE;
      AutoHome "Gateway lost";
    ENDIF
  ENDPROC

  PROC Beat()
    hbLast := gw_heartbeat;
    hbSeen := TRUE;
    hbLost := FALSE;
    ClkReset hbClock;
    ClkStart hbClock;
  ENDPROC

  ! Starts a home job of the arm's own, at v50, as if it had been sent:
  ! move_request makes the gateway and CommServer see the arm busy. Not
  ! again until another job has run, and not while both arms hold
  ! something (a cup in a handover would be torn apart).
  PROC AutoHome(string reason)
    IF auto_home_left RETURN;
    IF holding_left AND holding_right THEN
      TPWrite reason + ": both arms hold, left arm stays";
      RETURN;
    ENDIF
    TPWrite reason + ": left arm going home";
    move_speed_left := [50, 500, 5000, 1000];
    move_acc_left := 100;
    move_ramp_left := 100;
    go_home_left := TRUE;
    grip_cmd_left := "";
    path_len_left := 0;
    auto_home_left := TRUE;
    autoJob := TRUE;
    move_request_left := TRUE;
  ENDPROC

  ! GoHomeLeft can stop early if blocked, so check where the arm ended up.
  FUNC bool AtHome()
    VAR jointtarget jt;
    jt := CJointT();
    RETURN Abs(jt.robax.rax_1 - home_left.robax.rax_1) < 1
       AND Abs(jt.robax.rax_2 - home_left.robax.rax_2) < 1
       AND Abs(jt.robax.rax_3 - home_left.robax.rax_3) < 1
       AND Abs(jt.robax.rax_4 - home_left.robax.rax_4) < 1
       AND Abs(jt.robax.rax_5 - home_left.robax.rax_5) < 1
       AND Abs(jt.robax.rax_6 - home_left.robax.rax_6) < 1
       AND Abs(jt.extax.eax_a - home_left.extax.eax_a) < 1;
  ENDFUNC

  ! CalcJointT raises an error if the target can't be reached.
  ! CHANGED: keeps the joint angles it found in jTarget, so the move uses exactly those
  FUNC bool Reachable(robtarget t)
    jTarget := CalcJointT(t, tool0 \WObj:=wobj0);
    RETURN TRUE;
  ERROR
    IF ERRNO = ERR_ROBLIMIT OR ERRNO = ERR_OUTSIDE_REACH THEN
      RETURN FALSE;
    ENDIF
  ENDFUNC

ENDMODULE