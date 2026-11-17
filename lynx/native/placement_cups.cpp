#include "placement_cups.hpp"
#include <opencv2/imgproc.hpp>
#include <algorithm>
#include <cmath>
#include <limits>
#include <map>
#include <queue>
#include <stdexcept>

namespace lynx::placement {
namespace {
bool finite_point(const cv::Vec3d& p) { return std::isfinite(p[0])&&std::isfinite(p[1])&&std::isfinite(p[2]); }
double median(std::vector<double> values) {
    std::sort(values.begin(),values.end());size_t n=values.size();
    return n%2?values[n/2]:(values[n/2-1]+values[n/2])/2;
}
struct Fringe { bool area_rejected=false; std::vector<cv::Point> pixels; };
Fringe fringe_radius(const cv::Mat& depth,const json& objects,const std::vector<int>& indices,
                     const cv::Mat& mask,int x0,int y0,const std::vector<cv::Vec3d>& seed_physical,
                     double radius,const cv::Matx33d& transform,const cv::Vec3d& translation,
                     const json& K,double table_z,double check_height) {
    Fringe result;std::vector<cv::Point2f> footprint;
    double min_height=std::numeric_limits<double>::infinity(),max_height=-min_height;int raised=0;
    for(const auto&p:seed_physical) {
        footprint.emplace_back(float(p[0]),float(p[1]));
        if(p[2]>table_z+30){++raised;min_height=std::min(min_height,p[2]);max_height=std::max(max_height,p[2]);}
    }
    if(raised<32)return result;
    std::vector<cv::Point2f> hull;cv::convexHull(footprint,hull);
    if(cv::contourArea(hull)<1)return result;
    cv::Mat singular;cv::SVD::compute(cv::Mat(transform),singular);
    const double min_scale=singular.at<double>(2),focal=std::max(K["fx"].get<double>(),K["fy"].get<double>());
    double min_optical=std::numeric_limits<double>::infinity();
    for(int y=0;y<mask.rows;++y)for(int x=0;x<mask.cols;++x)if(mask.at<unsigned char>(y,x)) {
        double value=depth_at(depth,y+y0,x+x0);if(std::isfinite(value)&&value>0)min_optical=std::min(min_optical,value);
    }
    double pad_value=std::ceil(radius*focal/(min_scale*min_optical))+2;
    if(!std::isfinite(pad_value)||pad_value>80)return result;
    int pad=int(pad_value),left=std::max(0,x0-pad),top=std::max(0,y0-pad);
    int right=std::min(depth.cols,x0+mask.cols+pad),bottom=std::min(depth.rows,y0+mask.rows+pad);
    cv::Size size(right-left,bottom-top);if(size.area()>131072)return result;
    cv::Mat body=cv::Mat::zeros(size,CV_8U),protected_mask=body.clone();
    mask.copyTo(body(cv::Rect(x0-left,y0-top,mask.cols,mask.rows)));
    for(size_t i=0;i<objects.size();++i) {
        if(std::find(indices.begin(),indices.end(),int(i))!=indices.end())continue;
        auto region=decode_mask(objects[i].value("segmentation",json()),depth.size());
        if(region.valid()) {
            cv::Rect overlap=cv::Rect(left,top,size.width,size.height)&cv::Rect(region.x,region.y,region.mask.cols,region.mask.rows);
            if(overlap.area()>0)for(int y=overlap.y;y<overlap.br().y;++y)for(int x=overlap.x;x<overlap.br().x;++x)
                protected_mask.at<unsigned char>(y-top,x-left)|=region.mask.at<unsigned char>(y-region.y,x-region.x);
        } else {
            cv::Vec4d box;if(!valid_box(objects[i],box))continue;
            int a=std::max(left,int(std::floor(std::clamp(box[0],-1e8,1e8)))),b=std::max(top,int(std::floor(std::clamp(box[1],-1e8,1e8))));
            int c=std::min(right,int(std::ceil(std::clamp(box[2],-1e8,1e8)))+1),d=std::min(bottom,int(std::ceil(std::clamp(box[3],-1e8,1e8)))+1);
            if(a<c&&b<d)protected_mask(cv::Rect(a-left,b-top,c-a,d-b)).setTo(1);
        }
    }
    cv::Mat physical(size,CV_64FC3),usable=cv::Mat::zeros(size,CV_8U),seed=usable.clone();
    for(int y=0;y<size.height;++y)for(int x=0;x<size.width;++x) {
        double optical=depth_at(depth,y+top,x+left);
        auto p=physical_point(optical,x+left,y+top,transform,translation,K);physical.at<cv::Vec3d>(y,x)=p;
        bool ok=std::isfinite(optical)&&optical>0&&finite_point(p)&&p[2]-table_z>30&&p[2]-table_z<=check_height&&!protected_mask.at<unsigned char>(y,x);
        usable.at<unsigned char>(y,x)=ok;seed.at<unsigned char>(y,x)=ok&&body.at<unsigned char>(y,x);
    }
    if(cv::countNonZero(seed)<32)return result;
    cv::Mat inverse=1-body,background,distance;
    int n=cv::connectedComponents(inverse,background,8,CV_32S);std::vector<bool> exterior_labels(n,false);
    for(int x=0;x<size.width;++x){exterior_labels[background.at<int>(0,x)]=true;exterior_labels[background.at<int>(size.height-1,x)]=true;}
    for(int y=0;y<size.height;++y){exterior_labels[background.at<int>(y,0)]=true;exterior_labels[background.at<int>(y,size.width-1)]=true;}
    exterior_labels[0]=false;cv::distanceTransform(inverse,distance,cv::DIST_L2,cv::DIST_MASK_PRECISE);
    cv::Mat allowed=seed.clone();
    for(int y=0;y<size.height;++y)for(int x=0;x<size.width;++x) {
        if(!usable.at<unsigned char>(y,x)||!exterior_labels[background.at<int>(y,x)])continue;
        double optical=depth_at(depth,y+top,x+left);const auto&p=physical.at<cv::Vec3d>(y,x);
        if(distance.at<float>(y,x)*optical/focal*min_scale>radius||p[2]<min_height-5||p[2]>max_height+5)continue;
        if(cv::pointPolygonTest(hull,cv::Point2f(float(p[0]),float(p[1])),true)<-radius)continue;
        allowed.at<unsigned char>(y,x)=1;
    }
    cv::Mat reached=seed.clone(),steps(size,CV_32S,cv::Scalar(-1));std::queue<cv::Point> frontier;
    for(int y=0;y<size.height;++y)for(int x=0;x<size.width;++x)if(seed.at<unsigned char>(y,x)){steps.at<int>(y,x)=0;frontier.emplace(x,y);}
    while(!frontier.empty()) {
        auto p=frontier.front();frontier.pop();int next_step=steps.at<int>(p)+1;
        for(int dy=-1;dy<=1;++dy)for(int dx=-1;dx<=1;++dx) {
            if(!dx&&!dy)continue;int x=p.x+dx,y=p.y+dy;
            if(x<0||y<0||x>=size.width||y>=size.height||reached.at<unsigned char>(y,x)||!allowed.at<unsigned char>(y,x))continue;
            auto delta=physical.at<cv::Vec3d>(y,x)-physical.at<cv::Vec3d>(p);
            if(delta.dot(delta)>144)continue;
            if(next_step>=size.width+size.height)return Fringe{};
            reached.at<unsigned char>(y,x)=1;steps.at<int>(y,x)=next_step;frontier.emplace(x,y);
        }
    }
    for(int y=0;y<size.height;++y)for(int x=0;x<size.width;++x)
        if(reached.at<unsigned char>(y,x)&&!body.at<unsigned char>(y,x))result.pixels.emplace_back(x+left,y+top);
    if(result.pixels.size()>.25*cv::countNonZero(mask)){result.area_rejected=true;result.pixels.clear();}
    return result;
}
bool inside_part(const cv::Vec4d& part,const cv::Vec4d& parent,bool handle) {
    double margin=handle?.15*std::max(parent[2]-parent[0],parent[3]-parent[1]):0;
    double x=(part[0]+part[2])/2,y=(part[1]+part[3])/2;
    return parent[0]-margin<=x&&x<=parent[2]+margin&&parent[1]-margin<=y&&y<=parent[3]+margin;
}
}
CupResult assign_cups(const cv::Mat& depth,const json& objects,const json& locations,
                      const cv::Matx33d& transform,const cv::Vec3d& translation,const json& K,
                      double cell_size,double table_z,double check_height) {
    if(!objects.is_array())throw std::invalid_argument("Placement detections must be labelled objects.");
    CupResult result;result.owners=cv::Mat::zeros(depth.size(),CV_32S);
    // Bound geometry scratch before creating per-pixel point vectors. A
    // resource failure aborts assessment instead of weakening an exemption.
    constexpr size_t max_group_pixels=262144,max_decoded_pixels=8388608;
    size_t decoded_pixels=0;
    std::map<int,MaskRegion> decoded;std::map<int,cv::Vec4d> boxes,bodies;
    for(size_t i=0;i<objects.size();++i) {
        const auto& obj=objects[i];
        if(!obj.is_object()||!obj.contains("class")||!obj["class"].is_string()||obj["class"].get<std::string>().empty())throw std::invalid_argument("Placement detections must be labelled objects.");
        auto name=obj["class"].get<std::string>();cv::Vec4d box;
        if(name=="CUP"&&valid_box(obj,box))bodies[int(i)]=box;
        auto confidence=obj.value("confidence",json());
        if((name!="CUP"&&name!="CUP BASE"&&name!="CUP HANDLE")||!valid_box(obj,box)||!confidence.is_number())continue;
        double conf=confidence.get<double>();if(!std::isfinite(conf)||conf<.5||conf>1)continue;
        auto region=decode_mask(obj.value("segmentation",json()),depth.size());if(!region.valid())continue;
        decoded_pixels+=region.mask.total();
        if(region.mask.total()>max_group_pixels||decoded_pixels>max_decoded_pixels)
            throw std::runtime_error("Placement cup mask exceeds the native assessment resource limit.");
        std::vector<cv::Point> points;cv::findNonZero(region.mask,points);cv::Rect bounds=cv::boundingRect(points);
        if(bounds.x+region.x<box[0]-2||bounds.br().x-1+region.x>box[2]+2||bounds.y+region.y<box[1]-2||bounds.br().y-1+region.y>box[3]+2)continue;
        decoded[int(i)]=region;boxes[int(i)]=box;
    }
    std::vector<std::pair<int,std::vector<int>>> groups;std::vector<int> standalone;
    auto group_index=[&](int key)->int{for(size_t j=0;j<groups.size();++j)if(groups[j].first==key)return int(j);return -1;};
    for(const auto&item:bodies)if(decoded.count(item.first))groups.push_back({item.first,{item.first}});
    for(const auto&item:decoded) {
        int i=item.first;auto name=objects[i]["class"].get<std::string>();if(name=="CUP")continue;
        std::vector<int> parents;
        for(const auto&body:bodies)if(inside_part(boxes.at(i),body.second,name=="CUP HANDLE"))parents.push_back(body.first);
        if(parents.size()==1&&group_index(parents[0])>=0)groups[group_index(parents[0])].second.push_back(i);
        else if(parents.empty()&&name=="CUP BASE"&&objects[i].value("is_cup",json())==json(true))standalone.push_back(i);
    }
    for(int i:standalone) {
        const auto& region=decoded.at(i);std::vector<int> matches;
        for(int root:standalone) {
            if(group_index(root)<0)continue;const auto&other=decoded.at(root);
            cv::Rect overlap=cv::Rect(region.x,region.y,region.mask.cols,region.mask.rows)&cv::Rect(other.x,other.y,other.mask.cols,other.mask.rows);
            if(overlap.area()<=0)continue;int shared=0;
            for(int y=overlap.y;y<overlap.br().y;++y)for(int x=overlap.x;x<overlap.br().x;++x)
                shared+=bool(region.mask.at<unsigned char>(y-region.y,x-region.x)&&other.mask.at<unsigned char>(y-other.y,x-other.x));
            if(double(shared)/std::min(cv::countNonZero(region.mask),cv::countNonZero(other.mask))>=.7)matches.push_back(root);
        }
        if(matches.size()==1)groups[group_index(matches[0])].second.push_back(i);
        else if(matches.empty())groups.push_back({i,{i}});
    }
    const double half=cell_size/2;
    for(const auto&group:groups) {
        const auto&indices=group.second;int x0=depth.cols,y0=depth.rows,x1=0,y1=0;
        for(int i:indices){const auto&r=decoded.at(i);x0=std::min(x0,r.x);y0=std::min(y0,r.y);x1=std::max(x1,r.x+r.mask.cols);y1=std::max(y1,r.y+r.mask.rows);}
        if(size_t(y1-y0)*(x1-x0)>max_group_pixels)
            throw std::runtime_error("Placement cup group exceeds the native assessment resource limit.");
        cv::Mat mask=cv::Mat::zeros(y1-y0,x1-x0,CV_8U);
        for(int i:indices){const auto&r=decoded.at(i);cv::Mat target=mask(cv::Rect(r.x-x0,r.y-y0,r.mask.cols,r.mask.rows));cv::bitwise_or(target,r.mask,target);}
        int total=cv::countNonZero(mask);if(total<32)continue;
        std::vector<cv::Point> supported;std::vector<cv::Vec3d> physical;int valid=0;
        for(int y=0;y<mask.rows;++y)for(int x=0;x<mask.cols;++x)if(mask.at<unsigned char>(y,x)) {
            double optical=depth_at(depth,y+y0,x+x0);if(!std::isfinite(optical)||optical<=0)continue;++valid;
            auto p=physical_point(optical,x+x0,y+y0,transform,translation,K);
            if(finite_point(p)&&p[2]>=table_z-30&&p[2]<=table_z+check_height){supported.emplace_back(x,y);physical.push_back(p);}
        }
        if(valid<32||double(valid)/total<.95||double(physical.size())/total<.95)continue;
        std::vector<int> scores(locations.size(),0);
        for(size_t j=0;j<locations.size();++j)for(const auto&p:physical)
            if(std::abs(p[0]-locations[j]["x"].get<double>())<=half&&std::abs(p[1]-locations[j]["y"].get<double>())<=half)++scores[j];
        if(scores.empty())continue;int maximum=*std::max_element(scores.begin(),scores.end());
        if(double(maximum)/physical.size()<.5||std::count(scores.begin(),scores.end(),maximum)!=1)continue;
        int winner=int(std::find(scores.begin(),scores.end(),maximum)-scores.begin()),owner=winner+1;
        auto fringe=fringe_radius(depth,objects,indices,mask,x0,y0,physical,20,transform,translation,K,table_z,check_height);
        if(fringe.area_rejected)fringe=fringe_radius(depth,objects,indices,mask,x0,y0,physical,10,transform,translation,K,table_z,check_height);
        for(const auto&p:supported){int& previous=result.owners.at<int>(p.y+y0,p.x+x0);previous=(previous==0||previous==owner)?owner:-1;}
        for(const auto&p:fringe.pixels){int& previous=result.owners.at<int>(p);previous=(previous==0||previous==owner)?owner:-1;}
        result.consumed.insert(indices.begin(),indices.end());std::vector<double> px,py;
        for(const auto&p:physical){px.push_back(p[0]);py.push_back(p[1]);}
        json detection_ids=json::array();for(int i:indices)detection_ids.push_back(objects[i].value("detection_id",json(i)));
        result.assignments.push_back({{"location_id",locations[winner]["id"]},{"object_indices",indices},{"detection_ids",detection_ids},
            {"overlap_fraction",scalar_rounded(double(maximum)/physical.size(),4)},{"center_mm",{rounded(median(px),2),rounded(median(py),2)}},
            {"mask_pixels",total},{"depth_fringe_pixels",fringe.pixels.size()}});
    }
    return result;
}
}
