MODULE Module1
	CONST robtarget p10:=[[-10.44,-350.54,199.40],[0.0629343,-0.843397,-0.111239,-0.521869],[0,0,0,4],[-101.614,9E+9,9E+9,9E+9,9E+9,9E+9]];
	CONST robtarget home1:=[[-9.58,-182.61,198.63],[0.0660107,-0.842421,-0.111215,-0.523069],[0,0,0,4],[-101.964,9E+9,9E+9,9E+9,9E+9,9E+9]];

    VAR socketdev server_socket;
    VAR socketdev client_socket;
    VAR string received_msg;
    VAR robtarget dynamic_target;

    PROC main()
        
        g_Init;
        SocketCreate server_socket;
        SocketBind server_socket, "127.0.0.1", 1025;
        SocketListen server_socket;

        WHILE TRUE DO
            SocketAccept server_socket, client_socket \Time:=WAIT_MAX;
            SocketReceive client_socket \Str:=received_msg;
            TPWrite "Received: " + received_msg;

            IF ParseCoords(received_msg, dynamic_target) THEN
                MoveJ home1, v1000, z50, tool0;
               
                WaitTime 1;
                MoveJ dynamic_target, v1000, z50, tool0;
                WaitTime 1;
               
               
    WaitTime 1;
    g_GripOut;
    WaitTime 2;
    g_GripIn;
    
                WaitTime 1;
                MoveJ home1, v1000, z50, tool0;
                SocketSend client_socket \Str:="Done";
            ELSE
                SocketSend client_socket \Str:="ParseError";
            ENDIF

            SocketClose client_socket;
        ENDWHILE
    ENDPROC

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