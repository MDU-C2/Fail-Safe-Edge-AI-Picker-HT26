MODULE Module1
	CONST robtarget p10:=[[-10.44,-350.54,199.40],[0.0629343,-0.843397,-0.111239,-0.521869],[0,0,0,4],[-101.614,9E+9,9E+9,9E+9,9E+9,9E+9]];
	CONST robtarget home1:=[[-9.58,-182.61,198.63],[0.0660107,-0.842421,-0.111215,-0.523069],[0,0,0,4],[-101.964,9E+9,9E+9,9E+9,9E+9,9E+9]];

    VAR socketdev server_socket;
    VAR socketdev client_socket;
    VAR string received_msg;
    VAR robtarget dynamic_target;
    VAR bool move_ok;

    PROC main()
        SocketCreate server_socket;
        SocketBind server_socket, "127.0.0.1", 1025;
        SocketListen server_socket;

        MoveJ home1, v1000, z50, tool0;   ! go home once, at startup only

        MainLoop;
    ENDPROC

    PROC MainLoop()
        WHILE TRUE DO
            SocketAccept server_socket, client_socket \Time:=WAIT_MAX;
            SocketReceive client_socket \Str:=received_msg;
            TPWrite "Received: " + received_msg;

            IF ParseCoords(received_msg, dynamic_target) THEN
                move_ok := MoveToTarget(dynamic_target);

                IF move_ok THEN
                 
                    WaitTime 1;
                  
                    SocketSend client_socket \Str:="Done";
                ELSE
                    SocketSend client_socket \Str:="Unreachable";
                ENDIF
            ELSE
                SocketSend client_socket \Str:="ParseError";
            ENDIF

            SocketClose client_socket;
        ENDWHILE

    ERROR
        TPWrite "Unhandled error in MainLoop - recovering, ERRNO: " + NumToStr(ERRNO, 0);
        SocketClose client_socket;
        MoveJ home1, v1000, z50, tool0;
        RETRY;
    ENDPROC

    FUNC bool IsReachable(robtarget target)
        VAR jointtarget jt;

        jt := CalcJointT(target, tool0 \WObj:=wobj0);
        RETURN TRUE;

    ERROR
        RETURN FALSE;
    ENDFUNC

    !**************************************************************
    ! Description:
    !   Tries target directly first. If that fails, goes home and
    !   retries once from there. If it still fails, goes home and
    !   gives up (waits for next coordinate, no error raised).
    !**************************************************************
    FUNC bool MoveToTarget(robtarget target)
        VAR num attempt_count := 0;

        IF NOT IsReachable(target) THEN
            TPWrite "Target rejected - unreachable before attempting move";
            RETURN FALSE;
        ENDIF
        
        SetDO do_openGrip, 1;
        MoveJ target, v1000, z50, tool0;
        SetDO do_openGrip, 0;
        RETURN TRUE;

    ERROR
        attempt_count := attempt_count + 1;
        TPWrite "Move failed, attempt: " + NumToStr(attempt_count, 0);

        IF attempt_count = 1 THEN
            MoveJ home1, v1000, z50, tool0;
            RETRY;
        ELSE
            MoveJ home1, v1000, z50, tool0;
            RETURN FALSE;
        ENDIF
    ENDFUNC

    FUNC bool ParseCoords(string msg, INOUT robtarget target)
        VAR bool ok1;
        VAR bool ok2;
        VAR bool ok3;
        VAR num comma1;
        VAR num comma2;
        VAR num x;
        VAR num y;
        VAR num z;

        comma1 := StrFind(msg, 1, ",");
        comma2 := StrFind(msg, comma1+1, ",");

        ok1 := StrToVal(StrPart(msg, 1, comma1-1), x);
        ok2 := StrToVal(StrPart(msg, comma1+1, comma2-comma1-1), y);
        ok3 := StrToVal(StrPart(msg, comma2+1, StrLen(msg)-comma2), z);

        IF ok1 AND ok2 AND ok3 THEN
            target := [[x,y,z],[0.0629343,-0.843397,-0.111239,-0.521869],[0,0,0,4],[-101.614,9E+9,9E+9,9E+9,9E+9,9E+9]];
            RETURN TRUE;
        ELSE
            RETURN FALSE;
        ENDIF
    ENDFUNC
ENDMODULE