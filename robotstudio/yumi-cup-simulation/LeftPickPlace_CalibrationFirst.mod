MODULE Module1
    ! ABB left-arm calibration posture; ordinary simulation motion only.
    CONST jointtarget jCalibrationL := [[0,-130,30,0,40,0],[135,9E9,9E9,9E9,9E9,9E9]];

!    CONST jointtarget jA := [
!        [44.517187296,-81.837728792,-81.578470373,-38.716211794,138,0],
!        [-93.710100845,9E+09,9E+09,9E+09,9E+09,9E+09]
!    ];

!    CONST jointtarget jB := [
!        [91.764378729,-42.155110851,-18.863462557,159.173224151,138,0],
!        [-41.059055275,9E+09,9E+09,9E+09,9E+09,9E+09]
!    ];
    
    ! Original target retained for XYZ/orientation only: its eax_a is not a valid arm angle.
    CONST robtarget pPick := [[435.737373997,118.434157235,-39.932057554],
        [0.048832588,0.753506635,0.654176505,0.043545714],
        [0,-1,2,1],
        [4.469646338,9E+09,9E+09,9E+09,9E+09,9E+09]];

    CONST robtarget pApproach := [[435.737433452,118.392905121,45.388966193],[0.048832412,0.753506658,0.654176495,0.043545677],[0,0,2,1],[8.375408479,9E+09,9E+09,9E+09,9E+09,9E+09]];

    CONST jointtarget jApproach := [[27.28301647,21.79229399,9.552087002,-40.426134339,-11.158308439,151.270972605],[12.320440366,9E+09,9E+09,9E+09,9E+09,9E+09]];
    
    ! Original Cartesian target retained for reference; use captured targets for motion.
    CONST robtarget pApproachSafe := [[435.737466219,117.792000571,48.296253987],[0.048832246,0.753506723,0.654176435,0.043545621],[0,0,2,1],[12.320440366,9E+09,9E+09,9E+09,9E+09,9E+09]];
    

    ! SIMULATION ONLY: YuMi_Practice_VC left-arm practice.
    VAR jointtarget jApproachActual;
    VAR robtarget pApproachActual;
    PERS bool candidateFound := TRUE;
    PERS num bestAngle := -105;
    PERS num validAngles := 35;
    PERS robtarget pNearCandidate := [[435.737,118.434,-29.9321],
        [0.0488326,0.753507,0.654177,0.0435457],
        [0,-1,2,1],
        [-105,9E+9,9E+9,9E+9,9E+9,9E+9]];
    PERS robtarget pPickCandidate := [[435.737,118.434,-39.9321],
        [0.0488326,0.753507,0.654177,0.0435457],
        [0,-1,2,1],
        [-105,9E+9,9E+9,9E+9,9E+9,9E+9]];
    PERS jointtarget jNearCandidate := [[34.7432,39.6186,-13.7685,-57.0662,-6.3301,168.702],[7.67143,9E+9,9E+9,9E+9,9E+9,9E+9]];
    PERS jointtarget jPickCandidate := [[35.4564,42.253,-17.6548,-66.4752,-5.6077,178.242],[7.27062,9E+9,9E+9,9E+9,9E+9,9E+9]];

    PERS robtarget pNearPick := [[435.737,118.434,-29.932],
        [0.0488329,0.753505,0.654179,0.0435454],
        [0,-1,2,1],
        [-105,9E+9,9E+9,9E+9,9E+9,9E+9]];
    PERS jointtarget jNearPick := [[34.7432,39.6186,-13.7685,-57.0662,-6.3301,168.702],[7.67143,9E+9,9E+9,9E+9,9E+9,9E+9]];
    PERS bool nearTaught := TRUE;
    PERS bool pathValidated := TRUE;

    PERS num completedCycles := 5;
    PERS robtarget pPickReached := [[435.737,118.434,-39.9321],
        [0.0488326,0.753507,0.654177,0.0435457],
        [0,-1,2,1],
        [-105,9E+9,9E+9,9E+9,9E+9,9E+9]];
    PERS jointtarget jPickReached := [[35.4564,42.253,-17.6547,-66.4745,-5.60774,178.241],[7.27063,9E+9,9E+9,9E+9,9E+9,9E+9]];


    ! Manual station grip/Attach at pick; open/Detach at place.
    ! simStage is a sequence guard, not a grasp sensor.
    PERS num simStage := 0;
    PERS bool placePrepared := TRUE;
    PERS robtarget pPlace := [[435.737,178.434,-39.9321],
        [0.0488326,0.753507,0.654177,0.0435457],
        [0,-1,2,1],
        [-105,9E+9,9E+9,9E+9,9E+9,9E+9]];
    PERS robtarget pNearPlace := [[435.737,178.434,-29.9321],
        [0.0488326,0.753507,0.654177,0.0435457],
        [0,-1,2,1],
        [-105,9E+9,9E+9,9E+9,9E+9,9E+9]];
    PERS robtarget pTransferPlace := [[435.737,178.434,48.2963],
        [0.0488326,0.753507,0.654177,0.0435457],
        [0,-1,2,1],
        [-105,9E+9,9E+9,9E+9,9E+9,9E+9]];
    PERS jointtarget jPlace := [[43.7621,40.743,-19.7235,119.556,-1.15654,-3.89179],[5.56859,9E+9,9E+9,9E+9,9E+9,9E+9]];
    PERS jointtarget jNearPlace := [[43.3812,37.9058,-15.5885,51.0231,-1.13826,64.7309],[5.72358,9E+9,9E+9,9E+9,9E+9,9E+9]];
    PERS jointtarget jTransferPlace := [[40.1076,19.4121,7.971,-3.83144,-5.73747,120.245],[7.27464,9E+9,9E+9,9E+9,9E+9,9E+9]];


    ! Run with empty, open station gripper and object released.
    PERS bool placePathValidated := TRUE;
    ! Station Logic simulation commands; no physical gripper I/O.
    PERS bool simCloseRequest := FALSE;
    PERS bool simOpenRequest := FALSE;
    PERS bool simCloseDone := TRUE;
    PERS bool simOpenDone := TRUE;
    PERS num automaticCycles := 1;
    VAR bool simCommandOK;
    PROC CheckPlacePath()
        IF (simStage<>0) OR (AtJoint(jApproach)=FALSE) THEN
            TPWrite "Place check requires approach and no staged grasp";
            RETURN;
        ENDIF
        placePathValidated := FALSE;
        PreparePlace;
        ConfJ \On;
        ConfL \On;
        MoveAbsJ jTransferPlace \NoEOffs,v10,fine,Servo;
        MoveAbsJ jNearPlace \NoEOffs,v10,fine,Servo;
        MoveL pPlace,v5,fine,Servo\WObj:=wobj0;
        WaitTime 0.2;
        MoveL pNearPlace,v5,fine,Servo\WObj:=wobj0;
        MoveAbsJ jTransferPlace \NoEOffs,v10,fine,Servo;
        MoveAbsJ jApproach \NoEOffs,v10,fine,Servo;
        placePathValidated := TRUE;
        TPWrite "Place path and retreat complete";
    ENDPROC

    FUNC bool AtJoint(jointtarget expected)
        VAR jointtarget actual;
        actual := CJointT();
        RETURN Abs(actual.robax.rax_1-expected.robax.rax_1)<2 AND
            Abs(actual.robax.rax_2-expected.robax.rax_2)<2 AND
            Abs(actual.robax.rax_3-expected.robax.rax_3)<2 AND
            Abs(actual.robax.rax_4-expected.robax.rax_4)<2 AND
            Abs(actual.robax.rax_5-expected.robax.rax_5)<2 AND
            Abs(actual.robax.rax_6-expected.robax.rax_6)<2 AND
            Abs(actual.extax.eax_a-expected.extax.eax_a)<2;
    ENDFUNC

    PROC PreparePlace()
        VAR jointtarget jTest;
        placePrepared := FALSE;
        pPlace := Offs(pPickCandidate,0,60,0);
        pNearPlace := Offs(pPlace,0,0,10);
        pTransferPlace := pPlace;
        pTransferPlace.trans.z := 48.296254;
        jPlace := CalcJointT(pPlace,Servo\WObj:=wobj0);
        jNearPlace := CalcJointT(pNearPlace,Servo\WObj:=wobj0);
        jTransferPlace := CalcJointT(pTransferPlace,Servo\WObj:=wobj0);
        FOR dz FROM 0 TO 10 DO
            jTest := CalcJointT(Offs(pPlace,0,0,dz),Servo\WObj:=wobj0);
        ENDFOR
        placePrepared := TRUE;
        TPWrite "Place reachability checked without motion";
    ENDPROC

    PROC SimPickStop()
        IF simStage<>0 OR (AtJoint(jApproach)=FALSE) THEN
            TPWrite "Pick requires empty gripper at approach";
            RETURN;
        ENDIF
        PreparePlace;
        ConfJ \On;
        ConfL \On;
        MoveAbsJ jNearPick \NoEOffs,v10,fine,Servo;
        MoveL pPickCandidate,v5,fine,Servo\WObj:=wobj0;
        WaitTime 0.2;
        pPickReached := CRobT(\Tool:=Servo\WObj:=wobj0);
        jPickReached := CJointT();
        simStage := 1;
        TPWrite "Close and attach PickObject in station";
        Stop;
    ENDPROC

    PROC SimLiftStop()
        IF simStage<>1 OR (AtJoint(jPickReached)=FALSE) THEN
            TPWrite "Lift requires verified pick stage";
            RETURN;
        ENDIF
        ConfJ \On;
        ConfL \On;
        MoveL pNearPick,v5,fine,Servo\WObj:=wobj0;
        MoveAbsJ jApproach \NoEOffs,v10,fine,Servo;
        simStage := 2;
        TPWrite "Lift complete. Inspect attached object";
        Stop;
    ENDPROC

    PROC SimPlaceStop()
        IF simStage<>2 OR (placePrepared=FALSE) OR (AtJoint(jApproach)=FALSE) THEN
            TPWrite "Place requires lift at approach";
            RETURN;
        ENDIF
        ConfJ \On;
        ConfL \On;
        MoveAbsJ jTransferPlace \NoEOffs,v10,fine,Servo;
        MoveAbsJ jNearPlace \NoEOffs,v10,fine,Servo;
        MoveL pPlace,v5,fine,Servo\WObj:=wobj0;
        simStage := 3;
        TPWrite "Open gripper and detach object in station";
        Stop;
    ENDPROC

    PROC SimRetreatStop()
        IF simStage<>3 OR (AtJoint(jPlace)=FALSE) THEN
            TPWrite "Retreat requires completed place stage";
            RETURN;
        ENDIF
        ConfJ \On;
        ConfL \On;
        MoveL pNearPlace,v5,fine,Servo\WObj:=wobj0;
        MoveAbsJ jTransferPlace \NoEOffs,v10,fine,Servo;
        MoveAbsJ jApproach \NoEOffs,v10,fine,Servo;
        simStage := 0;
        TPWrite "Staged pick and place complete";
    ENDPROC

    ! Start using Simulation Play with the saved pick-position station state.
    ! Station Logic animates the fingers and attaches/releases PickObject.
    PROC SimGrip(bool closeGripper)
        VAR bool timedOut;
        simCommandOK := FALSE;
        simCloseRequest := FALSE;
        simOpenRequest := FALSE;
        WaitTime 0.15;
        timedOut := FALSE;
        IF closeGripper THEN
            simCloseDone := FALSE;
            simCloseRequest := TRUE;
            WaitUntil simCloseDone\MaxTime:=5\TimeFlag:=timedOut;
            simCloseRequest := FALSE;
        ELSE
            simOpenDone := FALSE;
            simOpenRequest := TRUE;
            WaitUntil simOpenDone\MaxTime:=5\TimeFlag:=timedOut;
            simOpenRequest := FALSE;
        ENDIF
        IF timedOut THEN
            TPWrite "Simulation grip timeout: use Simulation Play";
            RETURN;
        ENDIF
        simCommandOK := TRUE;
    ENDPROC

    PROC main()
        simStage := 0;
        simCloseRequest := FALSE;
        simOpenRequest := FALSE;
        simCloseDone := FALSE;
        simOpenDone := FALSE;
        IF NOT nearTaught THEN
            TPWrite "Near pick has not been taught";
            RETURN;
        ENDIF
        PreparePlace;
        ConfJ \On;
        ConfL \On;
        ! Saved initial state has open fingers and a released workpiece.
        simCommandOK := TRUE;
        IF NOT simCommandOK THEN
            RETURN;
        ENDIF
        TPWrite "Moving left arm to calibration posture";
        MoveAbsJ jCalibrationL \NoEOffs,v20,fine,Servo;
        WaitTime 1;
        TPWrite "Automatic pick and side-place cycle";
        MoveAbsJ jApproach \NoEOffs,v10,fine,Servo;
        MoveAbsJ jNearPick \NoEOffs,v10,fine,Servo;
        MoveL pPickCandidate,v5,fine,Servo\WObj:=wobj0;
        SimGrip TRUE;
        IF NOT simCommandOK THEN
            RETURN;
        ENDIF
        simStage := 1;
        MoveL pNearPick,v5,fine,Servo\WObj:=wobj0;
        MoveAbsJ jApproach \NoEOffs,v10,fine,Servo;
        simStage := 2;
        MoveAbsJ jTransferPlace \NoEOffs,v10,fine,Servo;
        MoveAbsJ jNearPlace \NoEOffs,v10,fine,Servo;
        MoveL pPlace,v5,fine,Servo\WObj:=wobj0;
        simStage := 3;
        SimGrip FALSE;
        IF NOT simCommandOK THEN
            RETURN;
        ENDIF
        MoveL pNearPlace,v5,fine,Servo\WObj:=wobj0;
        MoveAbsJ jTransferPlace \NoEOffs,v10,fine,Servo;
        MoveAbsJ jApproach \NoEOffs,v10,fine,Servo;
        simStage := 0;
        automaticCycles := automaticCycles+1;
        TPWrite "Automatic pick and side-place complete";
    ENDPROC

    PROC CheckPickPath()
        IF simStage<>0 THEN
            TPWrite "Complete staged release and retreat first";
            RETURN;
        ENDIF
        IF NOT nearTaught THEN
            TPWrite "Near pick has not been taught";
            RETURN;
        ENDIF
        pathValidated := FALSE;
        ConfJ \On;
        ConfL \On;
        MoveAbsJ jApproach \NoEOffs,v10,fine,Servo;
        MoveAbsJ jNearPick \NoEOffs,v10,fine,Servo;
        MoveL pPickCandidate,v5,fine,Servo\WObj:=wobj0;
        WaitTime 0.2;
        pPickReached := CRobT(\Tool:=Servo\WObj:=wobj0);
        jPickReached := CJointT();
        TPWrite "Pick pose reached";
        WaitTime 2;
        MoveL pNearPick,v5,fine,Servo\WObj:=wobj0;
        MoveAbsJ jApproach \NoEOffs,v10,fine,Servo;
        pathValidated := TRUE;
        completedCycles := completedCycles+1;
        TPWrite "Pick and retreat cycle complete";
    ENDPROC

    PROC FindPickCandidate()
        VAR robtarget pTest;
        VAR jointtarget jTest;
        VAR errnum solveError;
        VAR bool allReachable;
        VAR num bestScore;
        VAR num score;
        jApproachActual := CJointT();
        pApproachActual := CRobT(\Tool:=Servo\WObj:=wobj0);
        candidateFound := FALSE;
        validAngles := 0;
        bestScore := 1E9;
        FOR angle FROM -180 TO 180 STEP 5 DO
            allReachable := TRUE;
            FOR height FROM 0 TO 10 DO
                pTest := Offs(pPick,0,0,height);
                pTest.extax.eax_a := angle;
                solveError := 0;
                jTest := CalcJointT(pTest,Servo\WObj:=wobj0\ErrorNumber:=solveError);
                IF solveError <> 0 THEN
                    allReachable := FALSE;
                ENDIF
            ENDFOR
            IF allReachable THEN
                validAngles := validAngles+1;
                score := Abs(angle-pApproachActual.extax.eax_a);
                IF score < bestScore THEN
                    bestScore := score;
                    bestAngle := angle;
                    candidateFound := TRUE;
                    pPickCandidate := pPick;
                    pPickCandidate.extax.eax_a := angle;
                    pNearCandidate := Offs(pPickCandidate,0,0,10);
                    jNearCandidate := CalcJointT(pNearCandidate,Servo\WObj:=wobj0);
                    jPickCandidate := CalcJointT(pPickCandidate,Servo\WObj:=wobj0);
                ENDIF
            ENDIF
        ENDFOR
        TPWrite "Reachability scan complete. No motion.";
    ENDPROC
ENDMODULE