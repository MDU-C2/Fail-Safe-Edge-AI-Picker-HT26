// Native equivalents of placement_depth_regions, placement_depth_split,
// placement_depth_shared, placement_depth_group, and placement_cup_uncertainty.
// All thresholds and work limits follow those Python policies. Attribution uses
// observed samples only; it does not fill depth holes or identify hidden objects.
#include "placement_depth.hpp"
#include "placement_common.hpp"
#include <opencv2/imgproc.hpp>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <map>
#include <numeric>
#include <optional>
#include <set>
#include <string>
#include <tuple>
#include <utility>

namespace lynx::placement {
namespace {
constexpr std::size_t kMaxFrame = 8388608, kMaxRegion = 131072, kMaxCrop = 262144;
constexpr std::size_t kMaxWork = 4 * kMaxFrame;
using Pixels = std::vector<cv::Point>;
using Bin = std::pair<std::int64_t, std::int64_t>;
using Bins = std::set<Bin>;

bool number(const json& v) { return v.is_number() && std::isfinite(v.get<double>()); }
bool integer(const json& v) { return v.is_number_integer(); }
bool finite(const cv::Vec3d& v) {
    return std::isfinite(v[0]) && std::isfinite(v[1]) && std::isfinite(v[2]);
}
bool valid_global(const cv::Mat& depth, const json& objects, const json& locations,
                  const cv::Matx33d& matrix, const cv::Vec3d& origin, const json& K,
                  double cell, double table, double height) {
    if (depth.empty() || depth.dims != 2 || (depth.type() != CV_16U && depth.type() != CV_32F && depth.type() != CV_64F) || depth.total() > kMaxFrame ||
        !objects.is_array() || objects.size() > 128 || !locations.is_array() || locations.empty()) return false;
    for (const auto& o: objects)
        if (!o.is_object() || !o.contains("class") || !o["class"].is_string()) return false;
    for (double v: matrix.val) if (!std::isfinite(v)) return false;
    if (!finite(origin) || !K.is_object()) return false;
    for (auto key: {"fx", "fy", "cx", "cy"}) if (!K.contains(key) || !number(K[key])) return false;
    if (K["fx"].get<double>() <= 0 || K["fy"].get<double>() <= 0 || !std::isfinite(cell) ||
        !std::isfinite(table) || !std::isfinite(height) || cell < 20 || cell > 200 || height <= 30) return false;
    std::set<json> ids;
    for (const auto& p: locations) {
        if (!p.is_object() || !p.contains("id") || !integer(p["id"]) ||
            !p.contains("x") || !p.contains("y") || !number(p["x"]) || !number(p["y"]) ||
            !ids.insert(p["id"]).second) return false;
    }
    return true;
}
std::map<json, int> places_map(const json& locations) {
    std::map<json, int> out;
    for (std::size_t i=0; i<locations.size(); ++i) out[locations[i]["id"]] = int(i);
    return out;
}
cv::Mat zeros(cv::Size size) { return cv::Mat::zeros(size, CV_8U); }
cv::Mat inverse(const cv::Mat& mask) {
    cv::Mat result(mask.size(), CV_8U);
    for (int y=0; y<mask.rows; ++y) for (int x=0; x<mask.cols; ++x)
        result.at<uchar>(y,x) = !mask.at<uchar>(y,x);
    return result;
}
cv::Mat holes(const cv::Mat& mask) {
    cv::Mat bg(mask.rows+2, mask.cols+2, CV_8U, cv::Scalar(1));
    inverse(mask).copyTo(bg(cv::Rect(1,1,mask.cols,mask.rows)));
    cv::floodFill(bg, cv::Point(0,0), cv::Scalar(2), nullptr, cv::Scalar(), cv::Scalar(), 8);
    cv::Mat result = zeros(mask.size());
    for (int y=0; y<mask.rows; ++y) for (int x=0; x<mask.cols; ++x)
        result.at<uchar>(y,x) = bg.at<uchar>(y+1,x+1)==1;
    return result;
}
std::optional<cv::Rect> box_region(const json& obj, cv::Size shape) {
    cv::Vec4d box;
    if (!valid_box(obj, box)) return {};
    // Clamp in floating point before conversion, including distant finite boxes.
    double x0=std::max(0.,std::floor(box[0])), y0=std::max(0.,std::floor(box[1]));
    double x1=std::min(double(shape.width),std::ceil(box[2])+1), y1=std::min(double(shape.height),std::ceil(box[3])+1);
    if (x0 >= x1 || y0 >= y1) return {};
    return cv::Rect(int(x0),int(y0),int(x1-x0),int(y1-y0));
}
Pixels pixels(const cv::Mat& mask, cv::Point offset={}) {
    Pixels out;
    cv::findNonZero(mask, out);
    if (offset.x || offset.y) for (auto& p: out) p += offset;
    return out;
}
void append(Pixels& target, const Pixels& extra) { target.insert(target.end(), extra.begin(), extra.end()); }
cv::Mat distance_from(const cv::Mat& seed) {
    cv::Mat distance;
    cv::distanceTransform(inverse(seed), distance, cv::DIST_L2, cv::DIST_MASK_PRECISE);
    return distance;
}
void overlay(cv::Mat& target, cv::Point origin, const cv::Mat& source, cv::Point source_origin) {
    const cv::Rect overlap = cv::Rect(origin,target.size()) & cv::Rect(source_origin,source.size());
    if (overlap.empty()) return;
    const cv::Rect dst(overlap.tl()-origin, overlap.size()), src(overlap.tl()-source_origin,overlap.size());
    cv::bitwise_or(target(dst), source(src), target(dst));
}
double min_scale(const cv::Matx33d& matrix) {
    cv::Mat singular;
    cv::SVD::compute(cv::Mat(matrix), singular);
    return *std::min_element(singular.ptr<double>(),singular.ptr<double>()+singular.total());
}
bool near_hull(const cv::Vec3d& point, const std::vector<cv::Point2f>& hull, double radius) {
    bool positive=false, negative=false;
    double closest=std::numeric_limits<double>::infinity();
    for (std::size_t i=0; i<hull.size(); ++i) {
        const cv::Point2d a(hull[i]), b(hull[(i+1)%hull.size()]);
        const cv::Point2d edge=b-a, offset(point[0]-a.x,point[1]-a.y);
        const double length=edge.dot(edge);
        if (length <= 0) continue;
        const double cross=edge.x*offset.y-edge.y*offset.x;
        positive = positive || cross>1e-9; negative = negative || cross < -1e-9;
        const double along=std::clamp(offset.dot(edge)/length,0.,1.);
        const cv::Point2d delta=offset-along*edge;
        closest=std::min(closest,delta.dot(delta));
    }
    return !(positive && negative) || closest <= radius*radius+1e-9;
}
struct FrameGeometry {
    const cv::Mat& depth;
    const json& locations;
    const cv::Matx33d& matrix;
    const cv::Vec3d& origin;
    const json& K;
    double half;
    cv::Vec3d point(cv::Point p) const { return physical_point(depth_at(depth,p),p.x,p.y,matrix,origin,K); }
    std::optional<Bins> bins(const Pixels& coordinates) const {
        Bins out;
        for (auto p: coordinates) {
            const auto q=point(p);
            if (!finite(q) || std::abs(q[0])>std::ldexp(1.,50) || std::abs(q[1])>std::ldexp(1.,50)) return {};
            out.emplace(std::int64_t(std::floor(q[0]/2.)),std::int64_t(std::floor(q[1]/2.)));
        }
        return out;
    }
    std::vector<std::size_t> scores(const Bins& bins) const {
        std::vector<std::size_t> out(locations.size());
        for (std::size_t i=0;i<locations.size();++i) for (auto [x,y]:bins)
            if (std::abs((double(x)+.5)*2.-locations[i]["x"].get<double>())<=half &&
                std::abs((double(y)+.5)*2.-locations[i]["y"].get<double>())<=half) ++out[i];
        return out;
    }
};
struct Vote { std::size_t bins=0; std::vector<std::size_t> scores; };
std::optional<Vote> vote(const FrameGeometry& geo, const Pixels& coordinates) {
    auto b=geo.bins(coordinates);
    if (!b) return {};
    return Vote{b->size(),geo.scores(*b)};
}
bool agrees(const std::optional<Vote>& v, int owner, bool majority=false) {
    if (!v || !v->bins || !v->scores[owner]) return false;
    for (std::size_t i=0; i<v->scores.size(); ++i) if (int(i)!=owner && v->scores[i]>=v->scores[owner]) return false;
    return !majority || v->scores[owner] >= v->bins*.5;
}
bool shared_vote_agrees(const FrameGeometry& geo, const Pixels& part, const Pixels& boundary, int owner) {
    auto base=geo.bins(part), extra=geo.bins(boundary);
    if (!base || !extra || base->empty()) return false;
    Bins combined=*base; combined.insert(extra->begin(),extra->end());
    auto scores=geo.scores(combined);
    scores[owner]=geo.scores(*base)[owner];
    if (!scores[owner]) return false;
    for (std::size_t i=0;i<scores.size();++i) if (int(i)!=owner && scores[i]>=scores[owner]) return false;
    return true;
}
struct Split { std::vector<cv::Mat> parts; cv::Mat seam; };
std::optional<Split> split_component(const cv::Mat& component, const std::vector<cv::Mat>& original_seeds) {
    if (component.empty() || component.total()>kMaxRegion || original_seeds.size()<2 || original_seeds.size()>4) return {};
    cv::Mat count=zeros(component.size());
    for (const auto& seed: original_seeds) {
        if (seed.size()!=component.size() || seed.type()!=CV_8U) return {};
        for (int y=0;y<seed.rows;++y) for (int x=0;x<seed.cols;++x) {
            if (seed.at<uchar>(y,x) && !component.at<uchar>(y,x)) return {};
            count.at<uchar>(y,x)+=seed.at<uchar>(y,x)!=0;
        }
    }
    std::vector<cv::Mat> seeds,distances;
    std::vector<int> seed_counts;
    for (const auto& seed:original_seeds) {
        cv::Mat exclusive=zeros(component.size());
        for (int y=0;y<seed.rows;++y) for (int x=0;x<seed.cols;++x)
            exclusive.at<uchar>(y,x)=seed.at<uchar>(y,x) && count.at<uchar>(y,x)==1;
        const int n=cv::countNonZero(exclusive);
        if (n<32) return {};
        seeds.push_back(exclusive); seed_counts.push_back(n); distances.push_back(distance_from(exclusive));
    }
    cv::Mat nearest(component.size(),CV_32S,cv::Scalar(-1));
    for (int y=0;y<component.rows;++y) for (int x=0;x<component.cols;++x) {
        float first=std::numeric_limits<float>::infinity(), second=first;
        int index=-1;
        for (std::size_t i=0;i<seeds.size();++i) {
            const float d=distances[i].at<float>(y,x);
            if (d<first) { second=first; first=d; index=int(i); }
            else second=std::min(second,d);
        }
        if (component.at<uchar>(y,x) && second-first>1.f && count.at<uchar>(y,x)<=1) nearest.at<int>(y,x)=index;
        for (std::size_t i=0;i<seeds.size();++i) if (seeds[i].at<uchar>(y,x)) nearest.at<int>(y,x)=int(i);
    }
    Split result;
    cv::Mat claimed=zeros(component.size());
    for (std::size_t i=0;i<seeds.size();++i) {
        cv::Mat candidate=nearest==int(i), labels;
        cv::connectedComponents(candidate, labels,8,CV_32S);
        std::set<int> anchors;
        for (auto p:pixels(seeds[i])) if (labels.at<int>(p)) anchors.insert(labels.at<int>(p));
        cv::Mat part=zeros(component.size());
        for (int y=0;y<part.rows;++y) for (int x=0;x<part.cols;++x)
            part.at<uchar>(y,x)=anchors.count(labels.at<int>(y,x));
        int n=cv::countNonZero(part);
        if (!n || seed_counts[i]<n*.5) return {};
        result.parts.push_back(part); claimed |= part;
    }
    result.seam=zeros(component.size());
    for (int y=0;y<component.rows;++y) for (int x=0;x<component.cols;++x)
        result.seam.at<uchar>(y,x)=component.at<uchar>(y,x) && !claimed.at<uchar>(y,x) && count.at<uchar>(y,x)!=1;
    return result;
}
struct Boundary { int first,second; cv::Mat mask; };
std::vector<Boundary> shared_boundaries(const cv::Mat& component, const std::vector<cv::Mat>& seeds,
    const std::vector<cv::Mat>& parts, const cv::Mat& seam, const cv::Mat& owners, const cv::Mat& world) {
    const int total=cv::countNonZero(component);
    if (!total || parts.size()<2 || parts.size()>4 || seeds.size()!=parts.size()) return {};
    cv::Mat claimed=zeros(component.size()), exact=zeros(component.size());
    for (std::size_t i=0;i<parts.size();++i) {
        if (!cv::countNonZero(seeds[i])) return {};
        for (int y=0;y<component.rows;++y) for (int x=0;x<component.cols;++x) {
            if ((seeds[i].at<uchar>(y,x) && !parts[i].at<uchar>(y,x)) ||
                (parts[i].at<uchar>(y,x) && (!component.at<uchar>(y,x)||claimed.at<uchar>(y,x)))) return {};
        }
        exact |= seeds[i]; claimed |= parts[i];
    }
    std::vector<cv::Mat> distances,near,valid,adjacent,support;
    for (std::size_t i=0;i<parts.size();++i) {
        distances.push_back(distance_from(seeds[i]));
        cv::Mat dilated; cv::dilate(parts[i],dilated,cv::Mat::ones(5,5,CV_8U)); near.push_back(dilated.clone());
        cv::Mat v=zeros(component.size());
        for (int y=0;y<v.rows;++y) for (int x=0;x<v.cols;++x)
            v.at<uchar>(y,x)=parts[i].at<uchar>(y,x)&&owners.at<int>(y,x)>=0;
        valid.push_back(v); cv::dilate(v,dilated,cv::Mat::ones(3,3,CV_8U)); adjacent.push_back(dilated);
        support.push_back(zeros(component.size()));
    }
    cv::Mat candidate=zeros(component.size());
    for (int y=0;y<component.rows;++y) for (int x=0;x<component.cols;++x) {
        if (!seam.at<uchar>(y,x)||!component.at<uchar>(y,x)||exact.at<uchar>(y,x)||claimed.at<uchar>(y,x)||owners.at<int>(y,x)!=0) continue;
        float first=std::numeric_limits<float>::infinity(), second=first; int n=0;
        for (std::size_t i=0;i<parts.size();++i) {
            float d=distances[i].at<float>(y,x); second=std::min(second,std::max(first,d)); first=std::min(first,d);
            n+=near[i].at<uchar>(y,x)!=0;
        }
        candidate.at<uchar>(y,x)=(second-first<=1.f && n==2);
    }
    for (std::size_t i=0;i<parts.size();++i) for (auto p:pixels(candidate)) {
        if (!near[i].at<uchar>(p) || !finite(world.at<cv::Vec3d>(p))) continue;
        for (int dy=-2;dy<=2 && !support[i].at<uchar>(p);++dy) for (int dx=-2;dx<=2;++dx) {
            const cv::Point q=p+cv::Point(dx,dy);
            if ((!dx&&!dy)||q.x<0||q.y<0||q.x>=component.cols||q.y>=component.rows||!valid[i].at<uchar>(q)||!finite(world.at<cv::Vec3d>(q))) continue;
            const auto delta=world.at<cv::Vec3d>(p)-world.at<cv::Vec3d>(q);
            if (delta.dot(delta)<=144.) {support[i].at<uchar>(p)=1;break;}
        }
    }
    int accepted_total=0;
    std::vector<Boundary> result;
    for (std::size_t a=0;a<parts.size();++a) for (std::size_t b=a+1;b<parts.size();++b) {
        cv::Mat pair=candidate & near[a] & near[b],labels,stats,centroids;
        const int n=cv::connectedComponentsWithStats(pair,labels,stats,centroids,8,CV_32S);
        cv::Mat accepted=zeros(component.size());
        for (int label=1;label<n;++label) {
            bool touch_a=false,touch_b=false, supported=true;
            const cv::Rect rect(stats.at<int>(label,cv::CC_STAT_LEFT),stats.at<int>(label,cv::CC_STAT_TOP),stats.at<int>(label,cv::CC_STAT_WIDTH),stats.at<int>(label,cv::CC_STAT_HEIGHT));
            for (int y=rect.y;y<rect.y+rect.height;++y) for (int x=rect.x;x<rect.x+rect.width;++x) if (labels.at<int>(y,x)==label) {
                touch_a=touch_a||adjacent[a].at<uchar>(y,x);touch_b=touch_b||adjacent[b].at<uchar>(y,x);
                supported=supported&&support[a].at<uchar>(y,x)&&support[b].at<uchar>(y,x);
            }
            if (!touch_a||!touch_b||!supported) continue;
            for (int y=rect.y;y<rect.y+rect.height;++y) for (int x=rect.x;x<rect.x+rect.width;++x)
                if (labels.at<int>(y,x)==label) accepted.at<uchar>(y,x)=1;
            accepted_total+=stats.at<int>(label,cv::CC_STAT_AREA);
        }
        if (cv::countNonZero(accepted)) result.push_back({int(a),int(b),accepted});
    }
    return accepted_total<=total*.05 ? result : std::vector<Boundary>{};
}
std::optional<std::pair<cv::Mat,json>> shared_group_region(const cv::Mat& component,
    const std::vector<cv::Mat>& seeds,const std::vector<std::vector<cv::Point2f>>& hulls,
    const cv::Mat& world,const cv::Mat& optical,const cv::Mat& eligible,const json& locations,
    const std::vector<int>& owner_indices,double cell_size,double scale,double focal) {
    cv::Mat count=zeros(component.size());
    for (const auto& seed:seeds) for (auto p:pixels(seed)) ++count.at<uchar>(p);
    int component_pixels=cv::countNonZero(component), exclusive_count=0;
    std::vector<int> exclusives(seeds.size());
    Bins bins;
    for (int y=0;y<component.rows;++y) for (int x=0;x<component.cols;++x) {
        const cv::Point p(x,y); const bool c=component.at<uchar>(p);
        if (c||count.at<uchar>(p)) {
            const auto q=world.at<cv::Vec3d>(p);
            if (!finite(q)||std::abs(q[0])>std::ldexp(1.,50)||std::abs(q[1])>std::ldexp(1.,50)||!std::isfinite(depth_at(optical,p))||depth_at(optical,p)<=0) return {};
        }
        if (!c) continue;
        if (count.at<uchar>(p)==1) {
            ++exclusive_count; for (std::size_t i=0;i<seeds.size();++i) if (seeds[i].at<uchar>(p)) ++exclusives[i];
        }
        const auto q=world.at<cv::Vec3d>(p);bins.emplace(std::int64_t(std::floor(q[0]/2.)),std::int64_t(std::floor(q[1]/2.)));
    }
    if (!component_pixels||exclusive_count<component_pixels*.5||std::any_of(exclusives.begin(),exclusives.end(),[](int n){return n<32;})) return {};
    const double half=cell_size/2.;std::set<int> owner_set(owner_indices.begin(),owner_indices.end());
    std::size_t union_bins=0;
    for (auto [x,y]:bins) {
        for (int owner:owner_indices) if (std::abs((x+.5)*2.-locations[owner]["x"].get<double>())<=half&&std::abs((y+.5)*2.-locations[owner]["y"].get<double>())<=half) {++union_bins;break;}
    }
    if (union_bins<bins.size()*.5) return {};
    std::vector<cv::Mat> distances;for(const auto& seed:seeds) distances.push_back(distance_from(seed));
    cv::Mat selected=zeros(component.size());int foreign_count=0,foreign_supported=0;
    for (auto p:pixels(component)) {
        if (!eligible.at<uchar>(p)||count.at<uchar>(p)) continue;
        const auto q=world.at<cv::Vec3d>(p);bool foreign=false,supported=false;
        for (std::size_t i=0;i<locations.size();++i) if (!owner_set.count(int(i))&&std::abs(q[0]-locations[i]["x"].get<double>())<=half&&std::abs(q[1]-locations[i]["y"].get<double>())<=half) foreign=true;
        if (foreign) {
            ++foreign_count;
            for (std::size_t i=0;i<seeds.size();++i) {
                // Preserve NumPy's dtype-dependent ufunc rounding at this bound.
                double distance;
                if (optical.depth() == CV_64F) {
                    distance=double(distances[i].at<float>(p))*depth_at(optical,p)/focal*scale;
                } else {
                    float observed=distances[i].at<float>(p)*float(depth_at(optical,p));
                    observed=observed/float(focal);
                    observed=observed*float(scale);
                    distance=observed;
                }
                if (distance<=20.&&cv::pointPolygonTest(hulls[i],cv::Point2f(float(q[0]),float(q[1])),true)>=-20.) {supported=true;break;}
            }
            if(supported)++foreign_supported;
        }
        selected.at<uchar>(p)=!foreign||supported;
    }
    const int shared=cv::countNonZero(selected);if(!shared)return {};
    json evidence={{"component_pixels",component_pixels},{"exclusive_seed_fraction",double(exclusive_count)/component_pixels},
        {"owner_area_fraction",double(union_bins)/bins.size()},{"shared_pixels",shared},{"foreign_cell_pixels",foreign_count},
        {"foreign_cell_exempted_pixels",foreign_supported},{"foreign_cell_retained_pixels",foreign_count-foreign_supported},{"edge_tolerance_mm",20.}};
    return std::make_pair(selected,evidence);
}
} // namespace
DepthResult assign_depth_regions(const cv::Mat& depth,const json& objects,const json& locations,
    const cv::Mat& owners,const json& assignments,const cv::Matx33d& matrix,const cv::Vec3d& origin,
    const json& K,double cell_size,double table_z,double check_height,bool consume_owners) {
    DepthResult original{owners,assignments,{}};
    if (depth.empty() || depth.dims!=2 || (depth.type()!=CV_16U && depth.type()!=CV_32F && depth.type()!=CV_64F) || depth.total()>kMaxFrame ||
        owners.size()!=depth.size() || owners.type()!=CV_32S || !objects.is_array() || objects.size()>128 ||
        !assignments.is_array() || assignments.size()>128 || !locations.is_array() || locations.empty()) return original;
    for (const auto& o:objects) if(!o.is_object()||!o.contains("class")||!o["class"].is_string())return original;
    for (const auto& a:assignments)if(!a.is_object())return original;
    // Preserve original ownership separately only when the caller explicitly
    // relinquishes its buffer. Ordinary callers retain the historical immutable
    // behavior. A wider owner range uses the ordinary path without truncation.
    bool compact_original = false;
    if (consume_owners) {
        double minimum, maximum;
        cv::minMaxLoc(owners, &minimum, &maximum);
        compact_original = minimum >= -1 && maximum <= 64;
    }
    cv::Mat original_owners;
    if (compact_original) owners.convertTo(original_owners, CV_8S);
    else original_owners = owners;
    const auto original_owner = [&](cv::Point point) {
        return compact_original ? int(original_owners.at<signed char>(point))
                                : original_owners.at<int>(point);
    };
    DepthResult result{compact_original ? owners : owners.clone(),assignments,{}};
    const std::vector<std::string> metadata={"depth_region_pixels","depth_region_extra_pixels","depth_area_mm2",
        "depth_area_by_location_mm2","depth_area_fraction","depth_assignment_method","depth_seed_area_mm2",
        "depth_seed_area_fraction","depth_split_components","depth_split_pixels","depth_split_seam_pixels",
        "depth_shared_boundary_pixels","depth_shared_groups"};
    for(auto& record:result.assignments)for(const auto& key:metadata)record.erase(key);
    if(result.assignments.empty()||!valid_global(depth,objects,locations,matrix,origin,K,cell_size,table_z,check_height))return result;
    const auto places=places_map(locations);
    FrameGeometry geo{depth,locations,matrix,origin,K,cell_size/2.};
    std::size_t work=0;
    std::vector<MaskRegion> decoded;
    for(const auto& obj:objects){
        auto region=decode_mask(obj.value("segmentation",json()),depth.size());
        if(region.valid()){work+=region.mask.total();if(work>kMaxWork)return result;}
        decoded.push_back(region);
    }
    struct Group {int record;std::set<int> indices;cv::Point origin;cv::Mat mask;};
    std::vector<Group> groups;
    cv::Mat forbidden=zeros(depth.size());
    for(std::size_t r=0;r<result.assignments.size();++r){
        const auto& record=result.assignments[r];
        if(!record.contains("location_id")||!integer(record["location_id"])||!places.count(record["location_id"])||
           !record.contains("object_indices")||!record["object_indices"].is_array()||record["object_indices"].empty())continue;
        std::set<int> indices;bool valid=true;
        for(const auto& index:record["object_indices"]){
            if(!integer(index)||index<0||index>=objects.size()){valid=false;break;}
            int i=index.get<int>();if(!indices.insert(i).second||!decoded[i].valid()){valid=false;break;}
        }
        if(!valid)continue;
        cv::Rect bounds;bool first=true;
        for(int i:indices){cv::Rect rct(decoded[i].x,decoded[i].y,decoded[i].mask.cols,decoded[i].mask.rows);bounds=first?rct:(bounds|rct);first=false;}
        if(std::size_t(bounds.area())>kMaxRegion)continue;
        work+=bounds.area();if(work>kMaxWork)return result;
        cv::Mat mask=zeros(bounds.size());
        for(int i:indices)overlay(mask,bounds.tl(),decoded[i].mask,{decoded[i].x,decoded[i].y});
        cv::Mat hs=holes(mask);cv::bitwise_or(forbidden(bounds),hs,forbidden(bounds));
        groups.push_back({int(r),indices,bounds.tl(),mask});
    }
    if(groups.empty())return result;
    const double fx=K["fx"].get<double>(),fy=K["fy"].get<double>(),cx=K["cx"].get<double>(),cy=K["cy"].get<double>();
    cv::Mat raised=zeros(depth.size());
    for(int y=0;y<depth.rows;++y)for(int x=0;x<depth.cols;++x){
        const double d=depth_at(depth,y,x);
        const double z=(matrix(2,0)*(x-cx)/fx+matrix(2,1)*(y-cy)/fy+matrix(2,2))*d+origin[2]-table_z;
        raised.at<uchar>(y,x)=std::isfinite(d)&&d>0&&std::isfinite(z)&&z>30&&z<=check_height&&!forbidden.at<uchar>(y,x);
    }
    forbidden.release();
    cv::Mat labels,stats,centroids;
    cv::connectedComponentsWithStats(raised,labels,stats,centroids,8,CV_32S);raised.release();
    std::map<int,std::vector<std::pair<int,int>>> associations;
    std::vector<int> component_order;
    for(std::size_t g=0;g<groups.size();++g){
        std::map<int,int> counts;
        for(auto p:pixels(groups[g].mask,groups[g].origin))++counts[labels.at<int>(p)];
        for(auto [component,n]:counts)if(component){
            if(!associations.count(component))component_order.push_back(component);
            associations[component].emplace_back(int(g),n);
        }
    }
    auto rect_for=[&](int c){return cv::Rect(stats.at<int>(c,cv::CC_STAT_LEFT),stats.at<int>(c,cv::CC_STAT_TOP),stats.at<int>(c,cv::CC_STAT_WIDTH),stats.at<int>(c,cv::CC_STAT_HEIGHT));};
    std::map<int,std::set<int>> candidates;
    for(int c:component_order){
        const auto rect=rect_for(c);const auto& members=associations[c];const int area=stats.at<int>(c,cv::CC_STAT_AREA);
        if(area>int(kMaxRegion)||std::size_t(rect.area())>kMaxRegion||rect.x<=0||rect.y<=0||rect.br().x>=depth.cols||rect.br().y>=depth.rows||members.size()>4)continue;
        bool valid=true;std::set<json> ids;std::set<int> indices;
        for(auto [g,n]:members){if(n<32)valid=false;ids.insert(result.assignments[groups[g].record]["location_id"]);indices.insert(groups[g].indices.begin(),groups[g].indices.end());}
        if(!valid||(members.size()==1&&members[0].second<area*.5)||(members.size()>1&&ids.size()!=members.size()))continue;
        candidates[c]=indices;
    }
    for(std::size_t i=0;i<objects.size();++i){
        std::set<int> hit;
        if(decoded[i].valid())for(auto p:pixels(decoded[i].mask,{decoded[i].x,decoded[i].y}))hit.insert(labels.at<int>(p));
        else{
            auto box=box_region(objects[i],depth.size());if(!box)return result;
            work+=box->area();if(work>kMaxWork)return result;
            for(int y=box->y;y<box->br().y;++y)for(int x=box->x;x<box->br().x;++x)hit.insert(labels.at<int>(y,x));
        }
        for(int c:hit)if(candidates.count(c)&&!candidates[c].count(int(i)))candidates.erase(c);
    }
    for(const auto& [c,unused]:candidates){
        (void)unused;const auto n=associations[c].size();work+=std::size_t(rect_for(c).area())*(n==1?1:8+12*n);
    }
    if(work>kMaxWork)return result;
    std::set<int> needed;
    for(const auto& [c,unused]:candidates){(void)unused;for(auto [g,n]:associations[c]){(void)n;needed.insert(g);}}
    std::map<int,Vote> seed_votes;
    auto owner_of=[&](int g){return places.at(result.assignments[groups[g].record]["location_id"]);};
    for(int g:needed){
        Pixels coords;for(auto p:pixels(groups[g].mask,groups[g].origin))if(labels.at<int>(p)>0)coords.push_back(p);
        auto v=vote(geo,coords);if(agrees(v,owner_of(g),true))seed_votes[g]=*v;
    }
    struct Bundle {int component;std::map<int,Pixels> parts;bool split=false;Pixels seam;std::vector<SharedRegion> shared;};
    std::vector<Bundle> bundles;
    for(int c:component_order){
        if(!candidates.count(c))continue;
        std::vector<int> members;bool valid=true;
        for(auto [g,n]:associations[c]){(void)n;members.push_back(g);if(!seed_votes.count(g))valid=false;}
        if(!valid)continue;
        const auto rect=rect_for(c);cv::Mat region=labels(rect)==c;region/=255;
        std::vector<cv::Mat> partitions,seeds;cv::Mat seam;
        if(members.size()==1)partitions.push_back(region);
        else{
            for(int g:members){cv::Mat seed=zeros(rect.size());overlay(seed,rect.tl(),groups[g].mask,groups[g].origin);seed &= region;seeds.push_back(seed);}
            auto split=split_component(region,seeds);if(!split)continue;partitions=split->parts;seam=split->seam;
        }
        Bundle bundle;bundle.component=c;bundle.split=!seam.empty();
        for(std::size_t i=0;i<members.size();++i){
            auto coords=pixels(partitions[i],rect.tl());
            if(members.size()>1&&!agrees(vote(geo,coords),owner_of(members[i]))){valid=false;break;}
            bundle.parts[members[i]]=std::move(coords);
        }
        if(!valid)continue;
        if(bundle.split){
            bundle.seam=pixels(seam,rect.tl());
            cv::Mat world(rect.size(),CV_64FC3);bool world_valid=true;
            for(int y=0;y<rect.height;++y)for(int x=0;x<rect.width;++x){const auto q=geo.point({x+rect.x,y+rect.y});world.at<cv::Vec3d>(y,x)=q;if(!finite(q)||std::abs(q[0])>std::ldexp(1.,50)||std::abs(q[1])>std::ldexp(1.,50))world_valid=false;}
            if(world_valid){
                cv::Mat original_crop; original_owners(rect).convertTo(original_crop, CV_32S);
                for(std::size_t i=0;i<members.size();++i)for(auto p:pixels(partitions[i])){
                    int& previous=original_crop.at<int>(p);if(previous>0&&previous!=owner_of(members[i])+1)previous=-1;
                }
                for(const auto& boundary:shared_boundaries(region,seeds,partitions,seam,original_crop,world))
                    bundle.shared.push_back({{members[boundary.first],members[boundary.second]},pixels(boundary.mask,rect.tl())});
            }
            for(std::size_t i=0;i<members.size();++i){
                const int g=members[i];Pixels boundary;
                for(const auto& shared:bundle.shared)if(shared.owners.count(g))append(boundary,shared.pixels);
                if(boundary.empty())continue;
                if(cv::countNonZero(seeds[i])<(cv::countNonZero(partitions[i])+boundary.size())*.5||!shared_vote_agrees(geo,bundle.parts[g],boundary,owner_of(g))){bundle.shared.clear();break;}
            }
        }
        bundles.push_back(std::move(bundle));
    }
    struct Plan {Pixels coordinates;Vote area;};
    auto pooled=[&](const std::vector<Bundle>& active){
        std::map<int,Pixels> coords;for(const auto& bundle:active)for(const auto& [g,part]:bundle.parts)append(coords[g],part);
        std::map<int,Plan> plans;std::set<int> failed;
        for(auto& [g,part]:coords){auto v=part.size()<=kMaxRegion?vote(geo,part):std::optional<Vote>{};if(agrees(v,owner_of(g)))plans[g]={part,*v};else failed.insert(g);}
        return std::make_pair(plans,failed);
    };
    auto [plans,failed]=pooled(bundles);std::set<int> rejected=failed;
    bool changed=true;
    while(changed){changed=false;for(const auto& bundle:bundles)if(bundle.split){bool hit=false;for(const auto& [g,p]:bundle.parts){(void)p;if(rejected.count(g))hit=true;}if(hit)for(const auto& [g,p]:bundle.parts){(void)p;if(rejected.insert(g).second)changed=true;}}}
    if(!rejected.empty()){
        bundles.erase(std::remove_if(bundles.begin(),bundles.end(),[&](const Bundle& b){if(!b.split)return false;for(const auto& [g,p]:b.parts){(void)p;if(rejected.count(g))return true;}return false;}),bundles.end());
        plans=pooled(bundles).first;
    }
    std::map<int,Pixels> shared_by_group;
    for(const auto& bundle:bundles)for(const auto& shared:bundle.shared)for(int g:shared.owners)append(shared_by_group[g],shared.pixels);
    std::set<int> rejected_shared;
    for(const auto& [g,boundary]:shared_by_group)if(!plans.count(g)||plans[g].coordinates.size()+boundary.size()>kMaxRegion||!shared_vote_agrees(geo,plans[g].coordinates,boundary,owner_of(g)))rejected_shared.insert(g);
    for(auto& bundle:bundles){
        bool bad=false;for(const auto& shared:bundle.shared)for(int g:shared.owners)if(rejected_shared.count(g))bad=true;
        if(bad)bundle.shared.clear();
        for(const auto& shared:bundle.shared){std::set<int> owner_ids;for(int g:shared.owners)owner_ids.insert(owner_of(g)+1);result.shared.push_back({owner_ids,shared.pixels});}
        if(bundle.split)for(auto p:bundle.seam)result.owners.at<int>(p)=-1;
    }
    for(const auto& [g,plan]:plans){
        auto& record=result.assignments[groups[g].record];const int winner=owner_of(g),owner=winner+1;
        int extra=0,owned=0;
        for(auto p:plan.coordinates){int& previous=result.owners.at<int>(p);extra+=previous==0;previous=(previous==0||previous==owner)?owner:-1;owned+=previous==owner;}
        const auto& seed=seed_votes.at(g);json areas=json::array();
        for(std::size_t i=0;i<locations.size();++i)areas.push_back({{"location_id",locations[i]["id"]},{"area_mm2",double(plan.area.scores[i])*4.}});
        record["depth_assignment_method"]="connected_depth_area";record["depth_region_pixels"]=owned;record["depth_region_extra_pixels"]=extra;
        record["depth_area_mm2"]=double(plan.area.bins)*4.;record["depth_area_by_location_mm2"]=areas;record["depth_area_fraction"]=double(plan.area.scores[winner])/plan.area.bins;
        record["depth_seed_area_mm2"]=double(seed.bins)*4.;record["depth_seed_area_fraction"]=double(seed.scores[winner])/seed.bins;
        int count=0;std::size_t split_pixels=0,seam_pixels=0,shared_pixels=0;
        for(const auto& bundle:bundles)if(bundle.split&&bundle.parts.count(g)){
            ++count;split_pixels+=bundle.parts.at(g).size();seam_pixels+=bundle.seam.size();for(const auto& shared:bundle.shared)if(shared.owners.count(g))shared_pixels+=shared.pixels.size();
        }
        if(count){record["depth_split_components"]=count;record["depth_split_pixels"]=split_pixels;record["depth_split_seam_pixels"]=seam_pixels;record["depth_shared_boundary_pixels"]=shared_pixels;}
    }
    std::set<int> accepted;for(const auto& bundle:bundles)accepted.insert(bundle.component);
    struct Support {cv::Mat mask;std::vector<cv::Point2f> hull;};
    std::map<int,Support> support_cache;std::set<int> support_computed;std::map<int,std::size_t> shared_counts;
    for(const auto& [g,p]:plans)shared_counts[g]=p.coordinates.size();
    for(const auto& bundle:bundles)for(const auto& shared:bundle.shared)for(int g:shared.owners)shared_counts[g]+=shared.pixels.size();
    const double scale=min_scale(matrix);if(!std::isfinite(scale)||scale<=1e-12)return result;
    for(int c:component_order){
        if(!candidates.count(c)||accepted.count(c)||associations[c].size()<2||associations[c].size()>4)continue;
        std::vector<int> members;bool valid=true;for(auto [g,n]:associations[c]){(void)n;members.push_back(g);if(!seed_votes.count(g))valid=false;}if(!valid)continue;
        const auto rect=rect_for(c);cv::Mat region=labels(rect)==c;region/=255;
        std::vector<cv::Mat> seeds;std::vector<std::vector<cv::Point2f>> hulls;
        for(int g:members){
            const auto& group=groups[g];
            if(!support_computed.count(g)){
                support_computed.insert(g);cv::Mat mask=zeros(group.mask.size());std::vector<cv::Point2f> pts;bool world_valid=true;
                for(auto local:pixels(group.mask)){
                    const auto p=local+group.origin;double d=depth_at(depth,p);if(!std::isfinite(d)||d<=0)continue;
                    auto q=geo.point(p);if(!finite(q)||std::abs(q[0])>std::ldexp(1.,50)||std::abs(q[1])>std::ldexp(1.,50)){world_valid=false;break;}
                    if(q[2]-table_z>=-30.&&q[2]-table_z<=check_height&&original_owner(p)==owner_of(g)+1){mask.at<uchar>(local)=1;pts.emplace_back(float(q[0]),float(q[1]));}
                }
                if(world_valid&&pts.size()>=32){std::vector<cv::Point2f> hull;cv::convexHull(pts,hull);if(hull.size()>=3&&cv::contourArea(hull)>0)support_cache[g]={mask,hull};}
            }
            if(!support_cache.count(g)){valid=false;break;}
            cv::Mat seed=zeros(rect.size());overlay(seed,rect.tl(),support_cache[g].mask,group.origin);seeds.push_back(seed);hulls.push_back(support_cache[g].hull);
        }
        if(!valid)continue;
        cv::Mat supported=region.clone();for(const auto& seed:seeds)supported|=seed;
        const double nan=std::numeric_limits<double>::quiet_NaN();cv::Mat world(rect.size(),CV_64FC3,cv::Scalar(nan,nan,nan));
        for(auto local:pixels(supported)){auto q=geo.point(local+rect.tl());if(!finite(q)||std::abs(q[0])>std::ldexp(1.,50)||std::abs(q[1])>std::ldexp(1.,50)){valid=false;break;}world.at<cv::Vec3d>(local)=q;}
        if(!valid)continue;
        cv::Mat eligible=zeros(rect.size());for(int y=0;y<rect.height;++y)for(int x=0;x<rect.width;++x)eligible.at<uchar>(y,x)=original_owner({x+rect.x,y+rect.y})==0&&result.owners.at<int>(y+rect.y,x+rect.x)==0;
        std::vector<int> owner_indices;for(int g:members)owner_indices.push_back(owner_of(g));
        auto group=shared_group_region(region,seeds,hulls,world,depth(rect),eligible,locations,owner_indices,cell_size,scale,std::max(fx,fy));if(!group)continue;
        const auto n=group->second["shared_pixels"].get<std::size_t>();for(int g:members)if(shared_counts[g]+n>kMaxRegion)valid=false;if(!valid)continue;
        std::set<int> owner_ids;json location_ids=json::array();for(int i:owner_indices){owner_ids.insert(i+1);location_ids.push_back(locations[i]["id"]);}
        result.shared.push_back({owner_ids,pixels(group->first,rect.tl())});group->second["location_ids"]=location_ids;
        for(int g:members){shared_counts[g]+=n;auto& record=result.assignments[groups[g].record];if(!record.contains("depth_shared_groups"))record["depth_shared_groups"]=json::array();record["depth_shared_groups"].push_back(group->second);}
    }
    return result;
}
std::pair<std::vector<SharedRegion>,json> cup_uncertainty_regions(const cv::Mat& depth,const json& objects,
    const json& locations,const json& assignments,const cv::Matx33d& matrix,const cv::Vec3d& origin,
    const json& K,double cell_size,double table_z,double check_height,double radius_mm,double tail_radius_mm) {
    const std::pair<std::vector<SharedRegion>,json> empty{{},json::array()};
    if(!valid_global(depth,objects,locations,matrix,origin,K,cell_size,table_z,check_height)||
       !assignments.is_array()||assignments.empty()||assignments.size()>32||locations.size()>128||
       check_height>160||!std::isfinite(radius_mm)||radius_mm<=0||radius_mm>40||
       !std::isfinite(tail_radius_mm)||tail_radius_mm<radius_mm||tail_radius_mm>80)return empty;
    for(const auto& a:assignments)if(!a.is_object())return empty;
    const double scale=min_scale(matrix);if(!std::isfinite(scale)||scale<=1e-12)return empty;
    const auto places=places_map(locations);FrameGeometry geo{depth,locations,matrix,origin,K,cell_size/2.};
    const double focal=std::max(K["fx"].get<double>(),K["fy"].get<double>());
    std::vector<MaskRegion> decoded;std::size_t work=0;cv::Mat protected_mask=zeros(depth.size());
    for(const auto& obj:objects){
        auto region=decode_mask(obj.value("segmentation",json()),depth.size());decoded.push_back(region);
        if(region.valid()){
            overlay(protected_mask,{0,0},region.mask,{region.x,region.y});work+=region.mask.total();
        }else{
            auto box=box_region(obj,depth.size());if(!box)return empty;protected_mask(*box).setTo(1);work+=box->area();
        }
        if(work>kMaxWork)return empty;
    }
    struct Group {json record;cv::Point origin;cv::Mat mask;std::set<int> indices;};
    std::vector<Group> groups;std::set<int> claimed;
    const std::set<std::string> cup_classes={"CUP","CUP BASE","CUP HANDLE"};
    for(const auto& record:assignments){
        if(!record.contains("location_id")||!integer(record["location_id"])||!places.count(record["location_id"])||
           !record.contains("object_indices")||!record["object_indices"].is_array()||record["object_indices"].empty())return empty;
        std::set<int> indices;
        for(const auto& index:record["object_indices"]){
            if(!integer(index)||index<0||index>=objects.size())return empty;
            int i=index.get<int>();if(!indices.insert(i).second||claimed.count(i))return empty;
        }
        claimed.insert(indices.begin(),indices.end());
        bool valid=true;
        for(int i:indices){const auto& obj=objects[i];if(!decoded[i].valid()||!cup_classes.count(obj["class"].get<std::string>())||!obj.contains("confidence")||!number(obj["confidence"])||obj["confidence"]<.5||obj["confidence"]>1)valid=false;}
        if(!valid)continue;
        cv::Rect bounds;bool first=true;
        for(int i:indices){cv::Rect r(decoded[i].x,decoded[i].y,decoded[i].mask.cols,decoded[i].mask.rows);bounds=first?r:(bounds|r);first=false;}
        if(std::size_t(bounds.area())>kMaxCrop)continue;
        cv::Mat mask=zeros(bounds.size());for(int i:indices)overlay(mask,bounds.tl(),decoded[i].mask,{decoded[i].x,decoded[i].y});
        cv::Mat hs=holes(mask);cv::bitwise_or(protected_mask(bounds),hs,protected_mask(bounds));
        work+=mask.total()*2;if(work>kMaxWork)return empty;
        groups.push_back({record,bounds.tl(),mask,indices});
    }
    // Signed storage is used only as a byte container; bit operations are uint32_t.
    cv::Mat memberships=cv::Mat::zeros(depth.size(),CV_32S);
    json evidence=json::array();std::vector<int> contributing;
    for(const auto& group:groups){
        const auto exact=pixels(group.mask,group.origin);if(exact.size()<32)continue;
        Pixels valid_pixels;std::vector<cv::Vec3d> seed_points;std::vector<cv::Point2f> hull_points;
        double min_optical=std::numeric_limits<double>::infinity(),maximum_z=-std::numeric_limits<double>::infinity();
        std::size_t raised=0;
        for(auto p:exact){
            const double d=depth_at(depth,p);const auto q=geo.point(p);
            if(std::isfinite(d)&&d>0&&finite(q)&&std::abs(q[0])<std::ldexp(1.,40)&&std::abs(q[1])<std::ldexp(1.,40)&&q[2]>=table_z-30.&&q[2]<=table_z+check_height){
                valid_pixels.push_back(p);seed_points.push_back(q);hull_points.emplace_back(float(q[0]),float(q[1]));min_optical=std::min(min_optical,d);maximum_z=std::max(maximum_z,q[2]);raised+=q[2]>table_z+30.;
            }
        }
        if(valid_pixels.size()<32||double(valid_pixels.size())/exact.size()<.95||raised<32)continue;
        std::vector<std::size_t> scores(locations.size());
        for(std::size_t i=0;i<locations.size();++i)for(const auto& p:seed_points)if(std::abs(p[0]-locations[i]["x"].get<double>())<=geo.half&&std::abs(p[1]-locations[i]["y"].get<double>())<=geo.half)++scores[i];
        const int owner=places.at(group.record["location_id"]);bool valid=scores[owner]>0&&scores[owner]>=seed_points.size()*.5;
        for(std::size_t i=0;i<scores.size();++i)if(int(i)!=owner&&scores[i]>=scores[owner])valid=false;if(!valid)continue;
        std::vector<cv::Point2f> hull;cv::convexHull(hull_points,hull);if(hull.size()<3||cv::contourArea(hull)<=0)continue;
        const double projected_pad=tail_radius_mm*focal/(scale*min_optical);
        if(!std::isfinite(projected_pad)||projected_pad>158)continue;
        const int pad=int(std::ceil(projected_pad))+2;
        const int left=std::max(0,group.origin.x-pad),top=std::max(0,group.origin.y-pad);
        const int right=std::min(depth.cols,group.origin.x+group.mask.cols+pad),bottom=std::min(depth.rows,group.origin.y+group.mask.rows+pad);
        const cv::Rect rect(left,top,right-left,bottom-top);
        if(std::size_t(rect.area())>kMaxCrop)continue;
        work+=std::size_t(rect.area())*6;if(work>kMaxWork)return empty;
        cv::Mat seed=zeros(rect.size());for(auto p:valid_pixels)seed.at<uchar>(p-rect.tl())=1;
        const cv::Mat distance=distance_from(seed);
        Pixels local_pixels;std::vector<cv::Vec3d> world;std::vector<double> image_distance;
        for(int y=0;y<rect.height;++y)for(int x=0;x<rect.width;++x){
            const cv::Point p(x+left,y+top);double d=depth_at(depth,p);
            const double dist=double(distance.at<float>(y,x))*d/focal*scale;
            if(dist<=tail_radius_mm&&std::isfinite(d)&&d>0&&!protected_mask.at<uchar>(p)){
                local_pixels.emplace_back(x,y);world.push_back(geo.point(p));image_distance.push_back(dist);
            }
        }
        if(local_pixels.empty())continue;
        const double ceiling=std::min(table_z+check_height,maximum_z+5.);
        std::vector<unsigned char> plausible(local_pixels.size()),edge(local_pixels.size()),tail(local_pixels.size());bool any_tail=false;
        for(std::size_t i=0;i<local_pixels.size();++i){
            const auto q=world[i];plausible[i]=finite(q)&&q[2]>table_z+30.&&q[2]<=ceiling&&std::abs(q[0])<std::ldexp(1.,40)&&std::abs(q[1])<std::ldexp(1.,40);
            edge[i]=plausible[i]&&image_distance[i]<=radius_mm&&near_hull(q,hull,radius_mm);
            bool outside=true;for(const auto& place:locations)if(std::abs(q[0]-place["x"].get<double>())<=geo.half&&std::abs(q[1]-place["y"].get<double>())<=geo.half)outside=false;
            tail[i]=plausible[i]&&!edge[i]&&outside&&q[2]<=table_z+70.&&near_hull(q,hull,tail_radius_mm);any_tail=any_tail||tail[i];
        }
        if(any_tail){
            cv::Mat path=zeros(rect.size()),anchors=zeros(rect.size());
            for(std::size_t i=0;i<local_pixels.size();++i)if(plausible[i]&&near_hull(world[i],hull,tail_radius_mm))path.at<uchar>(local_pixels[i])=1;
            for(std::size_t i=0;i<valid_pixels.size();++i)if(seed_points[i][2]>table_z+30.&&seed_points[i][2]<=ceiling)anchors.at<uchar>(valid_pixels[i]-rect.tl())=1;
            for(std::size_t i=0;i<decoded.size();++i){
                if(group.indices.count(int(i)))continue;
                if(decoded[i].valid()){
                    const auto& d=decoded[i];const cv::Rect overlap=rect&cv::Rect(d.x,d.y,d.mask.cols,d.mask.rows);
                    if(!overlap.empty()){
                        for(int y=overlap.y;y<overlap.br().y;++y)for(int x=overlap.x;x<overlap.br().x;++x)if(d.mask.at<uchar>(y-d.y,x-d.x))anchors.at<uchar>(y-top,x-left)=0;
                        work+=overlap.area();
                    }
                }else{
                    const auto box=box_region(objects[i],depth.size()); // Validated while protecting detections.
                    if(!box)return empty;
                    const cv::Rect overlap=rect&*box;if(!overlap.empty()){anchors(cv::Rect(overlap.tl()-rect.tl(),overlap.size())).setTo(0);work+=overlap.area();}
                }
                if(work>kMaxWork)return empty;
            }
            if(cv::countNonZero(anchors)){
                path|=anchors;cv::Mat connected;cv::connectedComponents(path,connected,8,CV_32S);
                std::set<int> anchor_labels;for(auto p:pixels(anchors))if(connected.at<int>(p))anchor_labels.insert(connected.at<int>(p));
                for(std::size_t i=0;i<tail.size();++i)tail[i]=tail[i]&&anchor_labels.count(connected.at<int>(local_pixels[i]));
            }else std::fill(tail.begin(),tail.end(),0);
        }
        std::size_t accepted=0,edge_count=0,tail_count=0;const std::uint32_t bit=std::uint32_t(1)<<contributing.size();
        for(std::size_t i=0;i<local_pixels.size();++i){
            edge_count+=edge[i];tail_count+=tail[i];if(!edge[i]&&!tail[i])continue;
            const auto p=local_pixels[i]+rect.tl();auto& value=memberships.at<int>(p);value=int(std::uint32_t(value)|bit);++accepted;
        }
        if(!accepted)continue;
        contributing.push_back(owner+1);
        evidence.push_back({{"location_id",group.record["location_id"]},{"object_indices",group.record["object_indices"]},
            {"uncertain_pixels",accepted},{"radius_mm",radius_mm},{"edge_uncertain_pixels",edge_count},
            {"projected_tail_pixels",tail_count},{"tail_radius_mm",tail_radius_mm},{"tail_maximum_height_mm",std::min(70.,ceiling-table_z)},
            {"maximum_height_mm",ceiling-table_z},{"seed_pixels",valid_pixels.size()},
            {"seed_overlap_fraction",double(scores[owner])/seed_points.size()},{"method","observed_cup_local_uncertainty"}});
    }
    // Ordering matches NumPy's stable sort by unsigned membership bitset.
    std::map<std::uint32_t,Pixels> combined;
    for(int y=0;y<depth.rows;++y)for(int x=0;x<depth.cols;++x){const auto value=std::uint32_t(memberships.at<int>(y,x));if(value)combined[value].emplace_back(x,y);}
    if(combined.empty()||combined.size()>256)return empty;
    std::vector<SharedRegion> regions;
    for(const auto& [value,coords]:combined){std::set<int> owners;for(std::size_t i=0;i<contributing.size();++i)if(value&(std::uint32_t(1)<<i))owners.insert(contributing[i]);regions.push_back({owners,coords});}
    return {regions,evidence};
}
} // namespace lynx::placement
