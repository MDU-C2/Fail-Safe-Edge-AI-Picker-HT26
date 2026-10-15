MODULE Module1

    ! Home pose = calibration position from the IRB 14000 manual.
    ! Axis 7 is the first external axis value: -135 for the right arm.
    CONST jointtarget home_right := [[0,-130,30,0,40,0],[-135,9E+09,9E+09,9E+09,9E+09,9E+09]];

    PROC main()
        GoHomeRight;
    ENDPROC

    PROC GoHomeRight()
        VAR jointtarget jt;

        ! Preferred: let the controller plan all axes together.
        IF IsCollFree(home_right) THEN
            MoveAbsJ home_right \NoEOffs, v200, fine, tool0;
            RETURN;
        ENDIF

        ! Blocked. Free the arm one axis at a time, starting with axis 7,
        ! then the wrist inwards. Blocked axes are skipped, not retried.
        jt := CJointT();

        jt.extax.eax_a := home_right.extax.eax_a;
        TryMove jt;

        jt.robax.rax_6 := home_right.robax.rax_6;
        TryMove jt;

        jt.robax.rax_5 := home_right.robax.rax_5;
        TryMove jt;

        jt.robax.rax_4 := home_right.robax.rax_4;
        TryMove jt;

        jt.robax.rax_3 := home_right.robax.rax_3;
        TryMove jt;

        jt.robax.rax_2 := home_right.robax.rax_2;
        TryMove jt;

        jt.robax.rax_1 := home_right.robax.rax_1;
        TryMove jt;

        ! Axes that were skipped may be clear now that others have moved.
        IF IsCollFree(home_right) THEN
            MoveAbsJ home_right \NoEOffs, v200, fine, tool0;
        ELSE
            TPWrite "GoHomeRight: blocked, move the arm clear by hand.";
        ENDIF
    ENDPROC

    ! Move only if the target is collision-free; otherwise skip it.
    PROC TryMove(jointtarget target)
        IF IsCollFree(target) THEN
            MoveAbsJ target \NoEOffs, v200, fine, tool0;
        ENDIF
    ENDPROC

ENDMODULE