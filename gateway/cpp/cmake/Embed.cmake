# ================================================================
# Embed a text file into the binary as a std::string_view
# ================================================================
#
# gateway_embed_file(<input> <symbol> <output.cpp>) writes
#
#   namespace gw::embedded { extern const std::string_view <symbol>; }
#
# at configure time; editing the input re-runs the configure step.
#

function(gateway_embed_file input symbol output)
    file(READ ${input} content)

    #
    # A raw string literal, with a delimiter that cannot occur in
    # the JSON. Large literals are split, since compilers limit the
    # length of a single one.
    #
    string(LENGTH "${content}" length)

    set(chunk 16000)
    set(literals "")
    set(offset 0)

    while(offset LESS length)
        string(SUBSTRING "${content}" ${offset} ${chunk} piece)
        string(APPEND literals "R\"gw_embed(${piece})gw_embed\"\n")
        math(EXPR offset "${offset} + ${chunk}")
    endwhile()

    file(
        WRITE ${output}
        "#include <string_view>\n"
        "namespace gw::embedded {\n"
        "extern const std::string_view ${symbol};\n"
        "static const char ${symbol}_data[] =\n${literals};\n"
        "const std::string_view ${symbol}(${symbol}_data, sizeof(${symbol}_data) - 1);\n"
        "}\n"
    )

    set_property(
        DIRECTORY
        APPEND
        PROPERTY CMAKE_CONFIGURE_DEPENDS ${input}
    )
endfunction()
