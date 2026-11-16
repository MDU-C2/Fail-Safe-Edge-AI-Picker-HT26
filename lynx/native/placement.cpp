#include "placement.hpp"
#include "placement_common.hpp"
#include "placement_cups.hpp"
#include "placement_depth.hpp"
#include <opencv2/imgproc.hpp>
#include <algorithm>
#include <cmath>
#include <limits>
#include <set>
#include <stdexcept>

namespace lynx {
namespace {
using placement::json;
bool finite_number(const json& v) { return v.is_number()&&std::isfinite(v.get<double>()); }
double number(const json& v,const char* field) {
    if(!v.contains(field)||!finite_number(v[field]))throw std::invalid_argument(std::string("Invalid placement ")+field);
    return v[field].get<double>();
}
bool cluster(const cv::Mat& mask) {
    cv::Mat labels,stats,centroids;int count=cv::connectedComponentsWithStats(mask,labels,stats,centroids,8,CV_32S);
    for(int i=1;i<count;++i)if(stats.at<int>(i,cv::CC_STAT_AREA)>=8)return true;return false;
}
int floor_pixel(double value) { return int(std::floor(std::clamp(value,-1e8,1e8))); }
int ceil_pixel(double value) { return int(std::ceil(std::clamp(value,-1e8,1e8))); }
struct Geometry {
    cv::Matx33d transform,inverse;cv::Vec3d translation;double fx,fy,cx,cy,table_z,cell_size,min_scale;
    std::vector<cv::Point2d> project(const std::vector<cv::Vec3d>& points) const {
        std::vector<cv::Point2d> projected;
        for(const auto&p:points) {
            auto c=inverse*(p-translation);
            if(!std::isfinite(c[0])||!std::isfinite(c[1])||!std::isfinite(c[2])||c[2]<=0)return {};
            projected.emplace_back(c[0]*fx/c[2]+cx,c[1]*fy/c[2]+cy);
        }
        return projected;
    }
    bool clearly_behind(double depth,int u,int v,double x,double y) const {
        if(!std::isfinite(depth)||depth<=0)return false;
        static constexpr double corners[4][2]={{-.5,-.5},{.5,-.5},{.5,.5},{-.5,.5}};
        cv::Vec3d rays[4];double enter[4]={0,0,0,0},leave[4];
        for(int i=0;i<4;++i){rays[i]=transform*cv::Vec3d((u+corners[i][0]-cx)/fx,(v+corners[i][1]-cy)/fy,1);leave[i]=INFINITY;}
        double half=cell_size/2,lower[3]={x-half,y-half,table_z},upper[3]={x+half,y+half,table_z+160};
        double exit_bound=INFINITY;
        for(int axis=0;axis<3;++axis) {
            double min_direction=INFINITY,max_direction=-INFINITY,max_far=-INFINITY;
            for(int i=0;i<4;++i) {
                double direction=rays[i][axis],near,far;
                if(std::abs(direction)<1e-12){bool inside=lower[axis]<=translation[axis]&&translation[axis]<=upper[axis];near=inside?-INFINITY:INFINITY;far=inside?INFINITY:-INFINITY;}
                else {double first=(lower[axis]-translation[axis])/direction,last=(upper[axis]-translation[axis])/direction;near=std::min(first,last);far=std::max(first,last);}
                enter[i]=std::max(enter[i],near);leave[i]=std::min(leave[i],far);
                min_direction=std::min(min_direction,direction);max_direction=std::max(max_direction,direction);max_far=std::max(max_far,far);
            }
            exit_bound=std::min(exit_bound,(min_direction<=1e-12&&max_direction>=-1e-12)?INFINITY:max_far);
        }
        for(int i=0;i<4;++i)if(!std::isfinite(enter[i])||!std::isfinite(leave[i])||leave[i]<=enter[i])return false;
        bool outside_xy=false;
        for(int axis=0;axis<2;++axis) {
            bool below=true,above=true;
            for(int i=0;i<4;++i){double p=translation[axis]+rays[i][axis]*depth;below&=p<lower[axis];above&=p>upper[axis];}
            outside_xy|=below||above;
        }
        return outside_xy&&std::isfinite(exit_bound)&&depth>exit_bound+20/min_scale;
    }
};
Geometry geometry(const json& config,const cv::Mat& depth) {
    if(!config.is_object()||!config.contains("calibration")||!config.contains("intrinsics"))throw std::invalid_argument("Missing placement geometry.");
    const auto& cal=config.at("calibration");const auto& K=config.at("intrinsics");Geometry g;
    double scale=number(cal,"scale");if(scale<=0)throw std::invalid_argument("Invalid placement calibration scale.");
    g.table_z=config.value("table_z_mm",0.0);g.cell_size=config.value("cell_size_mm",80.0);
    if(!std::isfinite(g.table_z)||!std::isfinite(g.cell_size)||g.cell_size<20||g.cell_size>200)throw std::invalid_argument("Invalid placement cell size or table height.");
    if(!cal.contains("R")||!cal["R"].is_array()||cal["R"].size()!=3||!cal.contains("t")||!cal["t"].is_array()||cal["t"].size()!=3)throw std::invalid_argument("Invalid placement calibration transform.");
    auto offset=cal.value("tag_offset",json::array({0,0,0}));if(!offset.is_array()||offset.size()!=3)throw std::invalid_argument("Invalid tag offset.");
    for(int y=0;y<3;++y) {
        if(!cal["R"][y].is_array()||cal["R"][y].size()!=3||!finite_number(cal["t"][y])||!finite_number(offset[y]))throw std::invalid_argument("Invalid placement calibration transform.");
        for(int x=0;x<3;++x){if(!finite_number(cal["R"][y][x]))throw std::invalid_argument("Invalid rotation.");g.transform(y,x)=scale*cal["R"][y][x].get<double>();if(!std::isfinite(g.transform(y,x)))throw std::invalid_argument("Calibration overflow.");}
        g.translation[y]=cal["t"][y].get<double>()+offset[y].get<double>();if(!std::isfinite(g.translation[y]))throw std::invalid_argument("Calibration overflow.");
    }
    double det=cv::determinant(g.transform);if(!std::isfinite(det)||std::abs(det)<1e-9)throw std::invalid_argument("Placement calibration cannot be inverted.");
    g.inverse=g.transform.inv();cv::Mat singular;cv::SVD::compute(cv::Mat(g.transform),singular);g.min_scale=singular.at<double>(2);
    g.fx=number(K,"fx");g.fy=number(K,"fy");g.cx=number(K,"cx");g.cy=number(K,"cy");
    if(g.fx<=0||g.fy<=0||!K.contains("width")||!K.contains("height")||!K["width"].is_number_integer()||!K["height"].is_number_integer()
       ||K["width"]!=depth.cols||K["height"]!=depth.rows)throw std::invalid_argument("Placement depth image does not match camera intrinsics.");
    return g;
}
}

json assess_placement(const cv::Mat& input,const json& objects,const json& config) {
    if(input.empty()||input.channels()!=1||input.dims!=2||input.cols>4096||input.rows>4096||input.total()>8388608
        ||(input.depth()!=CV_16U&&input.depth()!=CV_32F&&input.depth()!=CV_64F))throw std::invalid_argument("Invalid bounded placement depth image.");
    const auto g=geometry(config,input);const auto& K=config.at("intrinsics");
    const auto& locations=config.at("grid_snapshot").at("locations");
    if(!locations.is_array()||locations.empty()||locations.size()>64)throw std::invalid_argument("Invalid placement locations.");
    std::set<int64_t> ids;
    for(const auto&place:locations) {
        if(!place.is_object()||!place.contains("id")||!place["id"].is_number_integer()||place["id"].get<int64_t>()<=0||!ids.insert(place["id"].get<int64_t>()).second
           ||!finite_number(place.value("x",json()))||!finite_number(place.value("y",json())))throw std::invalid_argument("Invalid placement location.");
    }
    if(!objects.is_array()||objects.size()>256)throw std::invalid_argument("Too many or invalid placement detections.");
    bool reference=config.contains("table_reference")&&!config["table_reference"].is_null();cv::Vec3d plane(0,0,0);
    if(reference) {
        const auto& ref=config["table_reference"];
        if(!ref.is_object()||!ref.contains("plane_z_mm")||!ref["plane_z_mm"].is_array()||ref["plane_z_mm"].size()!=3)throw std::invalid_argument("Invalid placement table reference.");
        for(int i=0;i<3;++i){if(!finite_number(ref["plane_z_mm"][i]))throw std::invalid_argument("Invalid reference plane.");plane[i]=ref["plane_z_mm"][i].get<double>();}
        if(std::atan(std::hypot(plane[0],plane[1]))*180/CV_PI>5)throw std::invalid_argument("Invalid reference slope.");
        for(const auto&place:locations)for(double dx:{-g.cell_size/2,g.cell_size/2})for(double dy:{-g.cell_size/2,g.cell_size/2}) {
            double expected=(place["x"].get<double>()+dx)*plane[0]+(place["y"].get<double>()+dy)*plane[1]+plane[2];
            if(!std::isfinite(expected)||std::abs(expected-g.table_z)>30)throw std::invalid_argument("Reference plane outside its bound.");
        }
    }
    const cv::Mat& depth=input;
    auto cups=placement::assign_cups(depth,objects,locations,g.transform,g.translation,K,g.cell_size,g.table_z,160);
    auto regions=placement::assign_depth_regions(depth,objects,locations,cups.owners,cups.assignments,g.transform,g.translation,K,g.cell_size,g.table_z,160,true);
    cups.owners.release();
    auto uncertainty=placement::cup_uncertainty_regions(depth,objects,locations,regions.assignments,g.transform,g.translation,K,g.cell_size,g.table_z,160);
    for(auto& assignment:regions.assignments)for(const auto&e:uncertainty.second)
        if(e["location_id"]==assignment["location_id"]&&e["object_indices"]==assignment["object_indices"]){assignment["depth_uncertainty"]=e;break;}
    cv::Mat protected_mask=cv::Mat::zeros(depth.size(),CV_8U);
    for(size_t i=0;i<objects.size();++i)if(!cups.consumed.count(int(i))) {
        auto r=placement::decode_mask(objects[i].value("segmentation",json()),depth.size());
        if(r.valid()){auto target=protected_mask(cv::Rect(r.x,r.y,r.mask.cols,r.mask.rows));cv::bitwise_or(target,r.mask,target);}
    }
    json slots=json::array();
    for(size_t index=0;index<locations.size();++index) {
        const auto&location=locations[index];double x=location["x"],y=location["y"],half=g.cell_size/2;
        std::vector<cv::Vec3d> corners={{x-half,y-half,g.table_z},{x+half,y-half,g.table_z},{x+half,y+half,g.table_z},{x-half,y+half,g.table_z}};
        auto floor=g.project(corners);for(size_t i=0;i<4;++i)corners.emplace_back(corners[i][0],corners[i][1],g.table_z+160);
        auto projected=g.project(corners);std::vector<cv::Point2f> volume,points;
        for(const auto&p:projected)points.emplace_back(float(p.x),float(p.y));if(!points.empty())cv::convexHull(points,volume);
        json polygon=json::array();for(const auto&p:floor)polygon.push_back({placement::rounded(p.x,2),placement::rounded(p.y,2)});
        json assigned=json::array();for(const auto&a:regions.assignments)if(a["location_id"]==location["id"])assigned.push_back(a);
        json slot={{"id",location["id"]},{"state","unknown"},{"reason","Placement area is outside the camera view."},{"reason_code","outside_view"},
            {"polygon_px",polygon},{"valid_fraction",0.0},{"reserved",location.value("reserved",json(false))},{"assigned_cups",assigned},
            {"cup_count",assigned.size()},{"evidence",assigned.empty()?json::array():json::array({"cup"})}};
        bool outside=volume.empty();for(const auto&p:volume)outside|=p.x<1||p.y<1||p.x>=depth.cols-1||p.y>=depth.rows-1;
        if(outside){slots.push_back(slot);continue;}
        double min_x=INFINITY,min_y=INFINITY,max_x=-INFINITY,max_y=-INFINITY;
        for(const auto&p:volume){min_x=std::min(min_x,double(p.x));min_y=std::min(min_y,double(p.y));max_x=std::max(max_x,double(p.x));max_y=std::max(max_y,double(p.y));}
        int x0=int(std::floor(min_x)),y0=int(std::floor(min_y)),x1=int(std::ceil(max_x))+1,y1=int(std::ceil(max_y))+1;
        cv::Rect bounds(x0,y0,x1-x0,y1-y0);cv::Mat roi=cv::Mat::zeros(bounds.size(),CV_8U),floor_roi=roi.clone();
        std::vector<cv::Point> raster;
        for(const auto&p:volume)raster.emplace_back(int(std::nearbyint(double(p.x)-x0)),int(std::nearbyint(double(p.y)-y0)));
        cv::fillConvexPoly(roi,raster,cv::Scalar(1));raster.clear();
        for(const auto&p:floor)raster.emplace_back(int(std::nearbyint(p.x-x0)),int(std::nearbyint(p.y-y0)));cv::fillConvexPoly(floor_roi,raster,cv::Scalar(1));
        int area=cv::countNonZero(roi),floor_area=cv::countNonZero(floor_roi);
        if(area<100){slot["reason"]="Placement area is too small in this image.";slot["reason_code"]="insufficient_projection";slots.push_back(slot);continue;}
        bool detected=false;
        for(size_t i=0;i<objects.size();++i)if(!cups.consumed.count(int(i))) {
            const auto box=objects[i].value("bbox_xyxy",json());if(!box.is_array()||box.size()!=4||!std::all_of(box.begin(),box.end(),finite_number))continue;
            int a=std::max(0,floor_pixel(box[0])-x0),b=std::max(0,floor_pixel(box[1])-y0),c=std::min(bounds.width,ceil_pixel(box[2])-x0+1),d=std::min(bounds.height,ceil_pixel(box[3])-y0+1);
            if(a<c&&b<d&&cv::countNonZero(roi(cv::Rect(a,b,c-a,d-b)))){detected=true;break;}
        }
        cv::Mat shared=cv::Mat::zeros(bounds.size(),CV_8U),uncertain=shared.clone();
        auto mark=[&](const auto& groups,cv::Mat& target){for(const auto&group:groups)if(!group.owners.count(int(index)+1))for(const auto&p:group.pixels)if(bounds.contains(p))target.at<unsigned char>(p.y-y0,p.x-x0)=1;};
        mark(regions.shared,shared);mark(uncertainty.first,uncertain);
        cv::Mat high=cv::Mat::zeros(bounds.size(),CV_8U),invalid=high.clone(),off_table=high.clone();
        int excluded=0,shared_excluded=0,uncertain_excluded=0,checked_count=0,checked_floor_count=0,valid_count=0,table_count=0,nominal_table_count=0;
        int raised=0,raised_inside=0,below=0,above=0,invalid_count=0;double minimum=INFINITY;
        for(int ry=0;ry<bounds.height;++ry)for(int rx=0;rx<bounds.width;++rx) {
            int u=rx+x0,v=ry+y0,owner=regions.owners.at<int>(v,u);bool in=roi.at<unsigned char>(ry,rx),in_floor=floor_roi.at<unsigned char>(ry,rx);
            bool ignored=in&&((owner>0&&owner!=int(index)+1)||shared.at<unsigned char>(ry,rx)||uncertain.at<unsigned char>(ry,rx))&&!protected_mask.at<unsigned char>(v,u);
            excluded+=ignored;shared_excluded+=ignored&&shared.at<unsigned char>(ry,rx);uncertain_excluded+=ignored&&uncertain.at<unsigned char>(ry,rx);
            bool checked=in&&!ignored,checked_floor=in_floor&&!ignored;checked_count+=checked;checked_floor_count+=checked_floor;
            double optical=placement::depth_at(depth,v,u);bool valid=in&&std::isfinite(optical)&&optical>0;valid_count+=valid&&checked;
            if(checked&&!valid){invalid.at<unsigned char>(ry,rx)=1;++invalid_count;}
            if(!valid||(!checked&&!checked_floor))continue;
            cv::Vec3d p;for(int axis=0;axis<3;++axis)p[axis]=(g.transform(axis,0)*(u-g.cx)/g.fx+g.transform(axis,1)*(v-g.cy)/g.fy+g.transform(axis,2))*optical+g.translation[axis];
            double height=p[2]-g.table_z;
            if(checked&&height>30&&!g.clearly_behind(optical,u,v,x,y)){high.at<unsigned char>(ry,rx)=1;++raised;raised_inside+=std::abs(p[0]-x)<=half&&std::abs(p[1]-y)<=half;}
            double residual=height;
            if(reference){double expected=std::clamp(plane[0]*p[0]+plane[1]*p[1]+plane[2],g.table_z-30,g.table_z+30);residual=height+g.table_z-std::min(expected,g.table_z);}
            if(checked_floor) {
                bool off=height>30||residual< -30;off_table.at<unsigned char>(ry,rx)=off;table_count+=!off;nominal_table_count+=std::abs(height)<=30;
                below+=residual< -30;above+=height>30;minimum=std::min(minimum,residual);
            }
        }
        double visible=double(checked_count)/area,floor_visible=double(checked_floor_count)/std::max(1,floor_area),fraction=double(valid_count)/std::max(1,checked_count),table_fraction=double(table_count)/std::max(1,checked_floor_count);
        slot.update({{"cup_excluded_pixels",excluded},{"cup_shared_excluded_pixels",shared_excluded},{"cup_uncertain_excluded_pixels",uncertain_excluded},
            {"visible_fraction",placement::scalar_rounded(visible,4)},{"floor_visible_fraction",placement::scalar_rounded(floor_visible,4)},{"valid_fraction",placement::scalar_rounded(fraction,4)},
            {"raised_depth_pixels",raised},{"raised_depth_inside_cell_pixels",raised_inside},{"raised_depth_outside_cell_pixels",raised-raised_inside},
            {"table_fraction",placement::scalar_rounded(table_fraction,4)},{"nominal_table_fraction",placement::scalar_rounded(double(nominal_table_count)/std::max(1,checked_floor_count),4)},
            {"table_reference_applied",reference},{"below_table_pixels",below},{"above_table_pixels",above},{"invalid_depth_pixels",invalid_count},
            {"minimum_floor_residual_mm",std::isfinite(minimum)?json(placement::scalar_rounded(minimum,2)):json()}});
        bool obstacle=cluster(high);if(detected)slot["evidence"].push_back("detected_object");if(obstacle)slot["evidence"].push_back("raised_depth");
        if(!assigned.empty()||detected||obstacle) {
            std::string reason;
            if(!assigned.empty()) {
                bool connected=std::all_of(assigned.begin(),assigned.end(),[](const json&a){return a.value("depth_assignment_method",json())=="connected_depth_area";});
                reason=std::string(connected?"Cup depth region assigned to P":"Cup assigned to P")+location["id"].dump()+(connected?" by its largest measured footprint.":" by its largest physical overlap.");
            }else reason=detected?"Object or unassigned cup visible in the placement area.":"Raised depth without an assigned cup; check for an object or inaccurate depth.";
            slot.update({{"state","occupied"},{"reason",reason},{"reason_code",!assigned.empty()?"cup":detected?"detected_object":"raised_depth"}});
        }else if(visible<.5||floor_visible<.5)slot.update({{"reason","Too much of the placement area is hidden by neighbouring cups."},{"reason_code","occluded"}});
        else if(fraction<.995||cluster(invalid))slot.update({{"reason","Not enough valid depth to confirm an empty table."},{"reason_code","invalid_depth"}});
        else if(table_fraction<.995||cluster(off_table))slot.update({{"reason",reference?"Depth does not consistently match the saved empty-table reference.":"Depth does not consistently match the calibrated table plane."},{"reason_code","table_depth"}});
        else slot.update({{"state","clear"},{"reason_code","clear"},{"reason",uncertain_excluded?"Visible table clear; nearby cup-edge depth uncertainty excluded.":excluded?"Visible table clear; segmented cup pixels belong to another cell.":"Clear table depth observed; no overlapping object detection."}});
        slots.push_back(std::move(slot));
    }
    return slots;
}
}
