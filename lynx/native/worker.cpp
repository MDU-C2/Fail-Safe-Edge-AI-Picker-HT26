#include "geometry.hpp"
#include "protocol.hpp"

#include <net.h>
#include <opencv2/core.hpp>
#include <opencv2/core/version.hpp>
#include <opencv2/imgproc.hpp>

#include <chrono>
#include <csignal>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <locale>
#include <sstream>
#include <string>
#include <utility>

namespace {

constexpr char kVersion[] = "1.0.0";

struct Options {
    std::string param, bin;
    int side = 0, classes = 0, threads = 1, max_detections = 32;
    float confidence = 0.5f, iou = 0.7f;
};

int integer(const std::string& value) {
    std::size_t end = 0;
    const int parsed = std::stoi(value, &end);
    if (end != value.size()) throw std::runtime_error("Invalid integer option");
    return parsed;
}

float real(const std::string& value) {
    std::size_t end = 0;
    const float parsed = std::stof(value, &end);
    if (end != value.size() || !std::isfinite(parsed)) throw std::runtime_error("Invalid float option");
    return parsed;
}

Options parse_options(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string key = argv[i];
        if (i + 1 >= argc) throw std::runtime_error("Missing value for " + key);
        const std::string value = argv[++i];
        if (key == "--param") options.param = value;
        else if (key == "--bin") options.bin = value;
        else if (key == "--input-size") options.side = integer(value);
        else if (key == "--classes-count") options.classes = integer(value);
        else if (key == "--threads") options.threads = integer(value);
        else if (key == "--confidence") options.confidence = real(value);
        else if (key == "--iou") options.iou = real(value);
        else if (key == "--max-detections") options.max_detections = integer(value);
        else throw std::runtime_error("Unknown option: " + key);
    }
    if (options.param.empty() || options.bin.empty() || options.side < 32 || options.side > 640 || options.side % 32)
        throw std::runtime_error("--param, --bin and an exported --input-size (32..640, multiple of 32) are required");
    if (options.classes < 3 || options.classes > 4)
        throw std::runtime_error("--classes-count must be 3 or 4; validate class mapping in the manifest");
    if (options.threads < 1 || options.threads > 4 || options.max_detections < 1 || options.max_detections > 64)
        throw std::runtime_error("Threads must be 1..4 and max detections 1..64");
    if (options.confidence <= 0 || options.confidence >= 1 || options.iou <= 0 || options.iou >= 1)
        throw std::runtime_error("Confidence and IoU must be strictly between 0 and 1");
    return options;
}

int anchor_count(int side) {
    return (side / 8) * (side / 8) + (side / 16) * (side / 16) + (side / 32) * (side / 32);
}

void bounded_model_file(const std::string& path, std::streamoff maximum) {
    std::ifstream stream(path, std::ios::binary | std::ios::ate);
    if (!stream || stream.tellg() <= 0 || stream.tellg() > maximum)
        throw std::runtime_error("Missing, empty or oversized model file: " + path);
}

// These fixed export reshape signatures must match before NCNN runs. A manifest
// cannot make a 640 graph into a 320 graph merely by changing its declared size.
void validate_export_graph(const Options& options) {
    bounded_model_file(options.param, 1024 * 1024);
    bounded_model_file(options.bin, 128 * 1024 * 1024);
    std::ifstream stream(options.param);
    std::string line;
    bool output_anchors = false, prototype_head = false;
    while (std::getline(stream, line)) {
        std::istringstream fields(line);
        std::string type, name;
        int inputs = 0, outputs = 0;
        if (!(fields >> type >> name >> inputs >> outputs) || type != "Reshape") continue;
        if (inputs < 0 || outputs < 0 || inputs + outputs > 64)
            throw std::runtime_error("Invalid export reshape record");
        std::string token;
        for (int i = 0; i < inputs + outputs; ++i) fields >> token;
        int width = 0, height = 0;
        while (fields >> token) {
            if (token.rfind("0=", 0) == 0) width = integer(token.substr(2));
            if (token.rfind("1=", 0) == 0) height = integer(token.substr(2));
        }
        output_anchors |= width == anchor_count(options.side) && height == 4;
        prototype_head |= width == (options.side / 8) * (options.side / 8) && height == lynx::kMaskChannels;
    }
    if (!output_anchors || !prototype_head)
        throw std::runtime_error("YOLO11 segmentation export graph does not match input-size; re-export the model");
}

void validate_float_tensor(const ncnn::Mat& mat, const char* name) {
    if (mat.empty() || mat.elempack != 1 || mat.elemsize != sizeof(float))
        throw std::runtime_error(std::string(name) + " must be unpacked FP32");
    // Channel stride may include alignment padding; inspect only actual elements.
    const int channels = mat.dims == 3 ? mat.c : 1;
    for (int k = 0; k < channels; ++k) {
        const ncnn::Mat channel = mat.dims == 3 ? mat.channel(k) : mat;
        for (int y = 0; y < mat.h; ++y) {
            const float* row = channel.row(y);
            for (int x = 0; x < mat.w; ++x)
                if (!std::isfinite(row[x]))
                    throw std::runtime_error(std::string(name) + " contains nonfinite values");
        }
    }
}

struct Result {
    std::string json;
    std::vector<unsigned char> masks;
};

class Segmenter {
public:
    explicit Segmenter(Options options) : options_(std::move(options)) {
        validate_export_graph(options_);
        net_.opt.num_threads = options_.threads;
        net_.opt.openmp_blocktime = 0;
        net_.opt.lightmode = true;
        net_.opt.use_vulkan_compute = false;
        net_.opt.use_fp16_packed = false;
        net_.opt.use_fp16_storage = false;
        net_.opt.use_fp16_arithmetic = false;
        net_.opt.use_bf16_storage = false;
        net_.opt.use_winograd_convolution = false;
        net_.opt.use_sgemm_convolution = false;
        net_.opt.use_int8_inference = true;
        if (net_.load_param(options_.param.c_str()) != 0 || net_.load_model(options_.bin.c_str()) != 0)
            throw std::runtime_error("NCNN model loading failed");
        const auto& inputs = net_.input_names();
        const auto& outputs = net_.output_names();
        if (inputs.size() != 1 || std::string(inputs[0]) != "in0" || outputs.size() != 2 ||
            !((std::string(outputs[0]) == "out0" && std::string(outputs[1]) == "out1") ||
              (std::string(outputs[1]) == "out0" && std::string(outputs[0]) == "out1")))
            throw std::runtime_error("Model must expose in0 -> out0,out1");
        cv::setNumThreads(1);
    }

    Result infer(const lynx::Request& request) {
        const auto start = std::chrono::steady_clock::now();
        const auto geometry = lynx::letterbox_geometry(request.width, request.height, options_.side);
        // Borrow request bytes; only the small model-sized image is copied.
        const cv::Mat bgr(request.height, request.width, CV_8UC3,
                          const_cast<unsigned char*>(request.bgr.data()));
        cv::Mat square(options_.side, options_.side, CV_8UC3, cv::Scalar(114, 114, 114));
        cv::Mat content = square(cv::Rect(geometry.left, geometry.top,
                                          geometry.resized_width, geometry.resized_height));
        cv::resize(bgr, content, content.size(), 0, 0, cv::INTER_LINEAR);
        ncnn::Mat input = ncnn::Mat::from_pixels(square.data, ncnn::Mat::PIXEL_BGR2RGB,
                                                 options_.side, options_.side);
        if (input.empty()) throw std::runtime_error("NCNN input allocation failed");
        constexpr float normalization[3] = {1.f / 255, 1.f / 255, 1.f / 255};
        input.substract_mean_normalize(nullptr, normalization);

        // Outputs are reference-counted NCNN Mats. Keep extractor and both Mats
        // alive for all postprocessing; never retain pointers across requests.
        auto extractor = net_.create_extractor();
        extractor.set_light_mode(true);
        ncnn::Mat prediction_storage, prototypes;
        if (extractor.input("in0", input) != 0 || extractor.extract("out0", prediction_storage) != 0 ||
            extractor.extract("out1", prototypes) != 0)
            throw std::runtime_error("NCNN inference failed");
        // channel() is a non-owning view in NCNN. Preserve prediction_storage,
        // including when extract() returned newly unpacked or converted storage.
        const ncnn::Mat prediction = prediction_storage.dims == 3 && prediction_storage.c == 1
                                        ? prediction_storage.channel(0) : prediction_storage;
        const int channels = 4 + options_.classes + lynx::kMaskChannels;
        const int anchors = anchor_count(options_.side);
        if (prediction.dims != 2 ||
            !((prediction.h == channels && prediction.w == anchors) ||
              (prediction.w == channels && prediction.h == anchors)))
            throw std::runtime_error("Unexpected out0 shape for this model input/classes");
        if (prototypes.dims != 3 || prototypes.c != lynx::kMaskChannels ||
            prototypes.w != options_.side / 4 || prototypes.h != options_.side / 4)
            throw std::runtime_error("Unexpected out1 prototype shape");
        validate_float_tensor(prediction, "out0");
        validate_float_tensor(prototypes, "out1");
        const bool channels_first = prediction.h == channels;
        const auto at = [&](int channel, int anchor) {
            return channels_first ? prediction.row(channel)[anchor] : prediction.row(anchor)[channel];
        };
        std::vector<lynx::Detection> candidates;
        candidates.reserve(anchors);
        for (int anchor = 0; anchor < anchors; ++anchor) {
            lynx::Detection detection;
            detection.score = at(4, anchor);
            for (int label = 0; label < options_.classes; ++label) {
                const float score = at(4 + label, anchor);
                if (score < 0 || score > 1) throw std::runtime_error("Class score is outside [0,1]");
                if (score > detection.score) { detection.score = score; detection.label = label; }
            }
            if (detection.score <= options_.confidence) continue;
            const float cx = at(0, anchor), cy = at(1, anchor), w = at(2, anchor), h = at(3, anchor);
            if (w <= 0 || h <= 0) continue;
            detection.box = {cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2};
            // Bound pathological finite outputs before IoU arithmetic can overflow.
            for (float value : detection.box)
                if (std::abs(value) > options_.side * 16)
                    throw std::runtime_error("Decoded box lies outside supported model coordinate range");
            for (int k = 0; k < lynx::kMaskChannels; ++k)
                detection.coefficients[k] = at(4 + options_.classes + k, anchor);
            candidates.push_back(detection);
        }
        const auto detections = lynx::class_aware_nms(std::move(candidates), options_.iou, options_.max_detections);
        Result result;
        std::ostringstream json;
        json.imbue(std::locale::classic());
        json << std::setprecision(std::numeric_limits<float>::max_digits10)
             << "{\"width\":" << request.width << ",\"height\":" << request.height << ",\"detections\":[";
        bool first = true;
        std::vector<float> logits(std::size_t(prototypes.w) * prototypes.h);
        for (const auto& detection : detections) {
            const auto box = lynx::original_box(detection, geometry);
            if (box[2] <= box[0] || box[3] <= box[1]) continue;
            std::fill(logits.begin(), logits.end(), 0.f);
            for (int k = 0; k < lynx::kMaskChannels; ++k) {
                const ncnn::Mat channel = prototypes.channel(k);
                for (int y = 0; y < prototypes.h; ++y) {
                    const float* source = channel.row(y);
                    float* destination = logits.data() + std::size_t(y) * prototypes.w;
                    for (int x = 0; x < prototypes.w; ++x)
                        destination[x] += detection.coefficients[k] * source[x];
                }
            }
            auto mask = lynx::original_mask(logits, prototypes.w, prototypes.h, box, request.width, request.height);
            if (std::none_of(mask.values.begin(), mask.values.end(), [](unsigned char x) { return x != 0; })) continue;
            if (mask.values.size() > lynx::kMaxMaskBytes - result.masks.size())
                throw std::runtime_error("Cropped masks exceed response memory budget");
            if (!first) json << ',';
            first = false;
            json << "{\"class_id\":" << detection.label << ",\"score\":" << detection.score
                 << ",\"box_xyxy\":[" << box[0] << ',' << box[1] << ',' << box[2] << ',' << box[3]
                 << "],\"mask_bbox\":[" << mask.x << ',' << mask.y << ',' << mask.width << ',' << mask.height
                 << "],\"mask_offset\":" << result.masks.size() << ",\"mask_length\":" << mask.values.size() << '}';
            result.masks.insert(result.masks.end(), mask.values.begin(), mask.values.end());
        }
        const auto elapsed = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
        json << "],\"inference_ms\":" << elapsed << '}';
        result.json = json.str();
        return result;
    }

private:
    Options options_;
    ncnn::Net net_;
};

}  // namespace

int main(int argc, char** argv) {
    // stdout belongs exclusively to the binary protocol; diagnostics use stderr.
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
#ifdef SIGPIPE
    std::signal(SIGPIPE, SIG_IGN);
#endif
    try {
        if (argc == 2 && std::string(argv[1]) == "--version") {
            std::cout << "lynx-ncnn-worker " << kVersion << " protocol=" << lynx::kProtocolVersion
                      << " ncnn=" << NCNN_VERSION_STRING << " opencv=" << CV_VERSION
                      << " postprocess=ultralytics-8.4.166-retina\n";
            return 0;
        }
        if (argc == 2 && std::string(argv[1]) == "--help") {
            std::cout << "lynx-ncnn-worker --param MODEL.param --bin MODEL.bin --input-size N --classes-count N\n"
                         "  [--threads 1] [--confidence 0.5] [--iou 0.7] [--max-detections 32]\n"
                         "Reads sequential LNXQ BGR frames on stdin; writes LNXR responses on stdout.\n";
            return 0;
        }
        const auto options = parse_options(argc, argv);
        Segmenter segmenter(options);
        {
            lynx::Request warmup;
            warmup.id = 1;
            warmup.width = warmup.height = options.side;
            warmup.bgr.assign(std::size_t(options.side) * options.side * 3, 114);
            (void)segmenter.infer(warmup);
        }
        lynx::write_response(std::cout, 0, 0,
            "{\"ready\":true,\"protocol_version\":1,\"version\":\"" + std::string(kVersion) +
            "\",\"ncnn_version\":\"" + NCNN_VERSION_STRING + "\",\"opencv_version\":\"" + CV_VERSION +
            "\",\"postprocess\":\"ultralytics-8.4.166-retina\",\"input_size\":" + std::to_string(options.side) +
            ",\"classes_count\":" + std::to_string(options.classes) + ",\"max_detections\":" +
            std::to_string(options.max_detections) + '}');
        lynx::Request request;
        while (lynx::read_request(std::cin, request)) {
            try {
                const auto result = segmenter.infer(request);
                lynx::write_response(std::cout, request.id, 0, result.json, result.masks);
            } catch (const std::bad_alloc&) {
                throw;  // Allocation failures terminate; do not reuse uncertain state.
            } catch (const std::exception& error) {
                lynx::write_response(std::cout, request.id, 1,
                    "{\"error\":" + lynx::json_string(error.what()) + '}');
            }
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "lynx-ncnn-worker: " << error.what() << '\n';
        return 1;
    }
}
