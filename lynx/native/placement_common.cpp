#include "placement_common.hpp"
#include <algorithm>
#include <cmath>
#include <iomanip>
#include <locale>
#include <sstream>
#include <string>
#include <vector>

namespace lynx::placement {
namespace {
bool finite(const json& v) { return v.is_number() && std::isfinite(v.get<double>()); }
bool integer(const json& v, int low, int high) {
    return v.is_number_integer() && v.get<double>() >= low && v.get<double>() <= high;
}
}
double rounded(double value, int places) {
    const double scale = std::pow(10.0, places);
    return std::nearbyint(value * scale) / scale;
}
double scalar_rounded(double value, int places) {
    // Python scalar round uses correctly rounded decimal conversion, unlike
    // NumPy array.round's multiply/rint/divide (2.675 -> 2.67 versus 2.68).
    std::ostringstream encoded;encoded.imbue(std::locale::classic());
    encoded<<std::fixed<<std::setprecision(places)<<value;
    std::istringstream decoded(encoded.str());decoded.imbue(std::locale::classic());
    double result;decoded>>result;return result;
}
cv::Vec3d physical_point(double optical, int x, int y, const cv::Matx33d& transform,
                        const cv::Vec3d& translation, const json& intrinsics) {
    const cv::Vec3d ray((x-intrinsics.at("cx").get<double>())/intrinsics.at("fx").get<double>(),
                       (y-intrinsics.at("cy").get<double>())/intrinsics.at("fy").get<double>(), 1.0);
    return (transform * ray) * optical + translation;
}
bool valid_box(const json& object, cv::Vec4d& box) {
    if (!object.is_object() || !object.contains("bbox_xyxy")) return false;
    const auto& v = object["bbox_xyxy"];
    if (!v.is_array() || v.size()!=4) return false;
    for (int i=0;i<4;++i) { if (!finite(v[i])) return false; box[i]=v[i].get<double>(); }
    return box[2]>box[0] && box[3]>box[1];
}
MaskRegion decode_mask(const json& value, cv::Size image_size) {
    MaskRegion empty;
    if (image_size.width<=0 || image_size.height<=0 || image_size.width>4096 || image_size.height>4096
        || int64_t(image_size.width)*image_size.height>4096*2160 || !value.is_object() || value.size()!=4
        || !value.contains("encoding") || value["encoding"]!="base64-packbits-big"
        || !value.contains("origin") || !value.contains("size") || !value.contains("data")) return empty;
    const auto& origin=value["origin"]; const auto& size=value["size"]; const auto& encoded=value["data"];
    if (!origin.is_array() || origin.size()!=2 || !size.is_array() || size.size()!=2 || !encoded.is_string()
        || !integer(origin[0],0,4096) || !integer(origin[1],0,4096)
        || !integer(size[0],1,4096) || !integer(size[1],1,4096)) return empty;
    int x=origin[0],y=origin[1],height=size[0],width=size[1];
    const size_t count=size_t(height)*width, byte_count=(count+7)/8;
    if (count>4096*2160 || y+height>image_size.height || x+width>image_size.width) return empty;
    const auto& data=encoded.get_ref<const std::string&>();
    if (data.size()!=((byte_count+2)/3)*4) return empty;
    std::vector<unsigned char> raw; raw.reserve(byte_count);
    auto digit=[](char c)->int {
        if(c>='A'&&c<='Z')return c-'A'; if(c>='a'&&c<='z')return c-'a'+26;
        if(c>='0'&&c<='9')return c-'0'+52; if(c=='+')return 62; if(c=='/')return 63; return -1;
    };
    for(size_t i=0;i<data.size();i+=4) {
        int a=digit(data[i]),b=digit(data[i+1]);
        int c=data[i+2]=='='?-2:digit(data[i+2]),d=data[i+3]=='='?-2:digit(data[i+3]);
        if(a<0||b<0||c==-1||d==-1||(c==-2&&d!=-2)||((c==-2||d==-2)&&i+4!=data.size()))return empty;
        if((c==-2&&(b&15))||(d==-2&&c>=0&&(c&3)))return empty;
        raw.push_back(static_cast<unsigned char>((a<<2)|(b>>4)));
        if(c>=0)raw.push_back(static_cast<unsigned char>((b<<4)|(c>>2)));
        if(d>=0)raw.push_back(static_cast<unsigned char>((c<<6)|d));
    }
    if(raw.size()!=byte_count || (count%8 && (raw.back()&((1u<<(8-count%8))-1))))return empty;
    cv::Mat mask(height,width,CV_8U); bool any=false;
    for(size_t i=0;i<count;++i) { unsigned char bit=(raw[i/8]>>(7-i%8))&1;mask.data[i]=bit;any|=bit!=0; }
    if(!any)return empty;
    return {x,y,mask};
}
}
