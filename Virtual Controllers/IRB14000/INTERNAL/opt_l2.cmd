

# Init VisionManager for Integrated Vision Option.
invoke -entry VisionManager_init -noarg

# Start VisionManager for Integrated Vision Option.
# int1 = camera communication thread priority
invoke2 -entry VisionManager_start -format int -int1 108

# Start discover thread 
task -slotname CameraDiscoverts -pri 108 -vwopt 0x8 -stcks 15000 -entp CameraDiscoverts_main -nosync -noreg 

# Add show routine to system dump service
sysdmp_add -name INTEGRATED_VISION -show ivshow4

# Add VisionLog ringbuffers to sysdmp
sysdmp_add -name INTEGRATED_VISION_LOG -source VisionLog -start VisionLog_ringbuffer_start -stop VisionLog_ringbuffer_stop -save VisionLog_ringbuffer_sysdmp_save

# Generate sysdump on "Problem with resultfilter"
sysdmp_trigger_add -name INTERNAL_ELOG_VISION_RESULTFILTER -elog_internal "Failed to find index in ResultFilter" -elog_generate_event 1 -elog_max_dumps 2
