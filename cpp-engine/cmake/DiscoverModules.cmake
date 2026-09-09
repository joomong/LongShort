# Each engine build has its own generated registry and external module sources.
# The generator writes its output only when content changes, so checking for new
# modules on every build does not recompile an unchanged engine.
function(longshort_discover_modules target_name modules_dir generator_script)
    find_package(Python3 3.8 REQUIRED COMPONENTS Interpreter)
    get_filename_component(module_path "${modules_dir}" ABSOLUTE)
    if(EXISTS "${module_path}" AND NOT IS_DIRECTORY "${module_path}")
        message(FATAL_ERROR "LONGSHORT_MODULE_DIR is not a directory: ${module_path}")
    endif()
    if(module_path MATCHES "[;\n\r]")
        message(FATAL_ERROR "LONGSHORT_MODULE_DIR cannot contain semicolons or newlines")
    endif()
    file(MAKE_DIRECTORY "${module_path}")
    get_filename_component(module_path "${module_path}" REALPATH)
    get_filename_component(binary_path "${CMAKE_BINARY_DIR}" REALPATH)
    cmake_path(IS_PREFIX module_path "${binary_path}" NORMALIZE build_inside_modules)
    if(build_inside_modules)
        message(FATAL_ERROR "Build directory must not be inside the module directory: ${module_path}")
    endif()
    file(GLOB_RECURSE module_headers CONFIGURE_DEPENDS LIST_DIRECTORIES false
        "${module_path}/*.h" "${module_path}/*.hpp")
    file(GLOB_RECURSE module_sources CONFIGURE_DEPENDS LIST_DIRECTORIES false
        "${module_path}/*.cpp" "${module_path}/*.cc" "${module_path}/*.cxx")

    # Match the generator: hidden path components disable a module and its sources.
    foreach(module_list module_headers module_sources)
        set(visible_files)
        foreach(module_file IN LISTS ${module_list})
            file(RELATIVE_PATH relative_path "${module_path}" "${module_file}")
            if(NOT relative_path MATCHES "(^|/)\\.")
                list(APPEND visible_files "${module_file}")
            endif()
        endforeach()
        set(${module_list} "${visible_files}")
    endforeach()

    set(registry_source "${CMAKE_CURRENT_BINARY_DIR}/generated/${target_name}_discovered_modules.cpp")
    add_custom_target(${target_name}_discover_modules
        COMMAND "${Python3_EXECUTABLE}" "${generator_script}"
            --modules-dir "${module_path}" --output "${registry_source}"
        BYPRODUCTS "${registry_source}"
        DEPENDS "${generator_script}" ${module_headers} ${module_sources}
        COMMENT "Discovering modules in ${module_path}"
        VERBATIM)
    add_dependencies(${target_name} ${target_name}_discover_modules)
    target_sources(${target_name} PRIVATE "${registry_source}" ${module_sources})
    target_include_directories(${target_name} PRIVATE "${module_path}")
    message(STATUS "LongShort module directory: ${module_path}")
endfunction()
