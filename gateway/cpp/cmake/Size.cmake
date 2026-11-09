# ================================================================
# Size optimisation and warnings
# ================================================================
#
# GATEWAY_SIZE applies to everything built in this tree, including
# the fetched dependencies (curl, GoogleTest), since they are built
# with the same flags.
#

if(GATEWAY_SIZE)
    #
    # -Os, plus one section per function and object so the linker
    # can drop everything unreferenced.
    #
    add_compile_options(
        -Os
        -ffunction-sections
        -fdata-sections
        -fvisibility=hidden
        $<$<COMPILE_LANGUAGE:CXX>:-fvisibility-inlines-hidden>
    )

    add_link_options(
        -Wl,--gc-sections
        -Wl,--as-needed
        -Wl,-O1
        -s
    )

    set(CMAKE_INTERPROCEDURAL_OPTIMIZATION ON)
endif()

function(gateway_warnings target)
    target_compile_options(
        ${target}
        PRIVATE
        -Wall
        -Wextra
        -Wpedantic
        -Wshadow
        -Wnon-virtual-dtor
    )
endfunction()
