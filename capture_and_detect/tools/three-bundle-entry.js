// Entry for web_static/3d/three-bundle.min.js: only the three.js parts the 3D
// view uses, as one self-contained ES module (the dashboard CSP allows no
// inline import map, and the Pi has no internet, so nothing loads from a CDN).
// Rebuild after changing the imports in web_static/3d/viewer3d.js:
//   npm install --no-save three@0.187.0 esbuild
//   npx esbuild capture_and_detect/tools/three-bundle-entry.js --bundle --format=esm --minify \
//     --legal-comments=inline --outfile=capture_and_detect/web_static/3d/three-bundle.min.js
export {
  AmbientLight, BufferAttribute, BufferGeometry, CanvasTexture, CircleGeometry, Color, CylinderGeometry,
  DirectionalLight, DoubleSide, GridHelper, Group, HemisphereLight, Mesh, MeshStandardMaterial,
  Object3D, PCFShadowMap, PerspectiveCamera, PlaneGeometry, SRGBColorSpace, Scene, Sprite,
  SpriteMaterial, TorusGeometry, Matrix4, Vector3, WebGLRenderer,
} from "three";
export { OrbitControls } from "three/addons/controls/OrbitControls.js";
