MODULE SharedVars

  PERS pos target_pos_left := [0, 0, 0];
  PERS orient target_rot_left := [1, 0, 0, 0];
  PERS bool use_rot_left := FALSE;
  PERS bool go_home_left := FALSE;
  PERS bool move_request_left := FALSE;
  PERS bool move_ok_left := FALSE;
  PERS string grip_cmd_left := "";            ! "" = nothing, "GRIP" = close, "OPEN" = open, "MOVE" = to grip_pos
  PERS num grip_pos_left := 25;               ! MOVE: opening between the fingers in mm, 0 (closed) .. 25 (open), g_MoveTo
  PERS num grip_force_left := 20;             ! GRIP: holding force in N, 0 .. 20 (SmartGripper default 20), g_GripIn
  PERS speeddata move_speed_left := [50, 500, 5000, 1000];  ! moves and home (v50)
  PERS num move_acc_left := 100;              ! AccSet acceleration, % of max (20..100)
  PERS num move_ramp_left := 100;             ! AccSet ramp (how fast acceleration builds up), % (10..100)

  ! Path job: path_len > 0 runs points 1..path_len in one go, blended
  ! through the middle points, stopping (fine) only at the last one.
  PERS num path_len_left := 0;
  PERS pos path_pos_left{8} := [[0,0,0],[0,0,0],[0,0,0],[0,0,0],[0,0,0],[0,0,0],[0,0,0],[0,0,0]];
  PERS orient path_rot_left{8} := [[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0]];
  PERS bool path_lin_left{8} := [FALSE,FALSE,FALSE,FALSE,FALSE,FALSE,FALSE,FALSE];  ! TRUE = straight line (MoveL)
  PERS num path_speed_left{8} := [50,50,50,50,50,50,50,50];   ! TCP speed to each point, mm/s
  PERS num path_zone_left{8} := [10,10,10,10,10,10,10,10];    ! blend radius at each point, mm (0 = stop there)
  PERS num path_acc_left := 0;                ! PathAccLim while on the path, m/s2 (0 = off), e.g. when carrying a cup

  PERS pos target_pos_right := [0, 0, 0];
  PERS orient target_rot_right := [1, 0, 0, 0];
  PERS bool use_rot_right := FALSE;
  PERS bool go_home_right := FALSE;
  PERS bool move_request_right := FALSE;
  PERS bool move_ok_right := FALSE;
  PERS string grip_cmd_right := "";
  PERS num grip_pos_right := 25;
  PERS num grip_force_right := 20;
  PERS speeddata move_speed_right := [50, 500, 5000, 1000];
  PERS num move_acc_right := 100;
  PERS num move_ramp_right := 100;

  PERS num path_len_right := 0;
  PERS pos path_pos_right{8} := [[0,0,0],[0,0,0],[0,0,0],[0,0,0],[0,0,0],[0,0,0],[0,0,0],[0,0,0]];
  PERS orient path_rot_right{8} := [[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0]];
  PERS bool path_lin_right{8} := [FALSE,FALSE,FALSE,FALSE,FALSE,FALSE,FALSE,FALSE];
  PERS num path_speed_right{8} := [50,50,50,50,50,50,50,50];
  PERS num path_zone_right{8} := [10,10,10,10,10,10,10,10];
  PERS num path_acc_right := 0;
       
ENDMODULE