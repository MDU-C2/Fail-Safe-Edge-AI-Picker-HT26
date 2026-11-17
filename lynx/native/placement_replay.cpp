// Offline differential-test executable. Not part of the deployed worker.
#ifdef LYNX_PLACEMENT_REPLAY_MAIN
#include "placement.hpp"
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <sys/resource.h>
int main() {
    std::string line;
    while(std::getline(std::cin,line)) {
        try {
            auto request=nlohmann::json::parse(line);int width=request.at("width"),height=request.at("height");
            if(width<=0||height<=0||width>4096||height>4096||int64_t(width)*height>8388608)throw std::invalid_argument("Invalid replay dimensions.");
            std::string dtype=request.value("depth_dtype","float64");
            int type=dtype=="uint16"?CV_16U:dtype=="float32"?CV_32F:dtype=="float64"?CV_64F:-1;
            if(type<0)throw std::invalid_argument("Invalid replay depth type.");
            cv::Mat depth(height,width,type);std::ifstream stream(request.at("depth_path").get<std::string>(),std::ios::binary);
            if(!stream.read(reinterpret_cast<char*>(depth.data),std::streamsize(depth.total()*depth.elemSize()))||stream.peek()!=EOF)throw std::invalid_argument("Invalid replay depth file.");
            auto slots=lynx::assess_placement(depth,request.at("objects"),request.at("config"));
            struct rusage usage{};getrusage(RUSAGE_SELF,&usage);
#ifdef __APPLE__
            double rss_mib=double(usage.ru_maxrss)/(1024*1024);
#else
            double rss_mib=double(usage.ru_maxrss)/1024;
#endif
            std::cout<<nlohmann::json({{"slots",slots},{"peak_rss_mib",rss_mib}}).dump()<<std::endl;
        } catch(const std::exception& error) {std::cout<<nlohmann::json({{"error",error.what()}}).dump()<<std::endl;}
    }
}
#endif
