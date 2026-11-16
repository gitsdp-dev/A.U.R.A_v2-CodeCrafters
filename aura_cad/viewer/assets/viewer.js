"use strict";
(() => {
  const canvas = document.getElementById("view");
  const message = document.getElementById("message");
  const gl = canvas.getContext("webgl", {antialias: false, alpha: false, preserveDrawingBuffer: true});
  if (!gl) {
    message.textContent = "This browser could not start WebGL. Try enabling hardware acceleration or update the browser.";
    return;
  }
  const vertexShader = `
    attribute vec3 position;
    attribute vec3 normal;
    uniform mat4 projection;
    uniform mat4 view;
    uniform float explode;
    varying float light;
    void main() {
      vec3 p = position + normal * explode;
      vec3 n = normalize(mat3(view) * normal);
      light = 0.28 + 0.72 * max(dot(n, normalize(vec3(0.35, 0.8, 0.6))), 0.0);
      gl_Position = projection * view * vec4(p, 1.0);
    }`;
  const fragmentShader = `
    precision mediump float;
    varying float light;
    uniform bool wireframe;
    void main() {
      vec3 color = wireframe ? vec3(0.40, 0.90, 0.82) : vec3(0.28, 0.68, 0.72) * light;
      gl_FragColor = vec4(color, 1.0);
    }`;
  const shader = (type, source) => {
    const item = gl.createShader(type);
    gl.shaderSource(item, source);
    gl.compileShader(item);
    if (!gl.getShaderParameter(item, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(item));
    return item;
  };
  const program = gl.createProgram();
  gl.attachShader(program, shader(gl.VERTEX_SHADER, vertexShader));
  gl.attachShader(program, shader(gl.FRAGMENT_SHADER, fragmentShader));
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    message.textContent = "The local WebGL viewer could not initialize.";
    return;
  }
  gl.useProgram(program);
  const locations = {
    position: gl.getAttribLocation(program, "position"),
    normal: gl.getAttribLocation(program, "normal"),
    projection: gl.getUniformLocation(program, "projection"),
    view: gl.getUniformLocation(program, "view"),
    explode: gl.getUniformLocation(program, "explode"),
    wireframe: gl.getUniformLocation(program, "wireframe")
  };
  let vertices = [], normals = [], positionBuffer = null, normalBuffer = null;
  let angleX = -0.5, angleY = 0.65, zoom = 2.8;
  let wireframe = false, exploded = false, grid = true, dragging = false, lastX = 0, lastY = 0;
  const buffer = (attribute, values) => {
    const item = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, item);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(values), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(attribute);
    gl.vertexAttribPointer(attribute, 3, gl.FLOAT, false, 0, 0);
    return item;
  };
  const perspective = (aspect, near, far) => {
    const f = 1 / Math.tan(Math.PI / 8), range = 1 / (near - far);
    return new Float32Array([
      f/aspect,0,0,0, 0,f,0,0, 0,0,(near+far)*range,-1, 0,0,near*far*range*2,0
    ]);
  };
  const viewMatrix = () => {
    const cx = Math.cos(angleX), sx = Math.sin(angleX), cy = Math.cos(angleY), sy = Math.sin(angleY);
    const d = zoom;
    return new Float32Array([
      cy, sx*sy, -cx*sy, 0, 0, cx, sx, 0, sy, -sx*cy, cx*cy, 0,
      0, 0, -d, 1
    ]);
  };
  const drawGrid = () => {
    if (!grid) return;
    const points = [];
    for (let n = -5; n <= 5; n += 1) {
      points.push(n/5, -0.55, -1, n/5, -0.55, 1, -1, -0.55, n/5, 1, -0.55, n/5);
    }
    const item = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, item);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(points), gl.STREAM_DRAW);
    gl.vertexAttribPointer(locations.position, 3, gl.FLOAT, false, 0, 0);
    gl.disableVertexAttribArray(locations.normal);
    gl.vertexAttrib3f(locations.normal, 0, 1, 0);
    gl.drawArrays(gl.LINES, 0, points.length / 3);
    gl.deleteBuffer(item);
    gl.enableVertexAttribArray(locations.normal);
  };
  let wireBuffer = null;
  const render = () => {
    const width = Math.max(1, canvas.clientWidth), height = Math.max(1, canvas.clientHeight);
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width; canvas.height = height;
      gl.viewport(0, 0, width, height);
    }
    gl.clearColor(0.035, 0.055, 0.07, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    gl.uniformMatrix4fv(locations.projection, false, perspective(width / height, 0.1, 100));
    gl.uniformMatrix4fv(locations.view, false, viewMatrix());
    gl.uniform1f(locations.explode, exploded ? 0.035 : 0);
    gl.uniform1i(locations.wireframe, wireframe ? 1 : 0);
    if (vertices.length) {
      gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
      gl.vertexAttribPointer(locations.position, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, normalBuffer);
      gl.vertexAttribPointer(locations.normal, 3, gl.FLOAT, false, 0, 0);
      if (wireframe && wireBuffer) {
        gl.bindBuffer(gl.ARRAY_BUFFER, wireBuffer);
        gl.vertexAttribPointer(locations.position, 3, gl.FLOAT, false, 0, 0);
        gl.drawArrays(gl.LINES, 0, vertices.length * 2 / 3);
      } else {
        gl.drawArrays(gl.TRIANGLES, 0, vertices.length / 3);
      }
    }
    drawGrid();
  };
  const stlVertices = (bytes) => {
    const view = new DataView(bytes);
    const count = view.getUint32(80, true);
    if (84 + count * 50 === bytes.byteLength) {
      const out = [];
      for (let i = 0, offset = 84; i < count; i += 1, offset += 50) {
        const n = [view.getFloat32(offset, true), view.getFloat32(offset+4, true), view.getFloat32(offset+8, true)];
        for (let j = 0; j < 3; j += 1) {
          out.push(view.getFloat32(offset+12+j*12, true), view.getFloat32(offset+16+j*12, true), view.getFloat32(offset+20+j*12, true));
          normals.push(...n);
        }
      }
      return out;
    }
    const text = new TextDecoder().decode(bytes), out = [];
    const values = text.match(/vertex\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+/g) || [];
    for (let i = 0; i + 2 < values.length; i += 3) {
      const tri = values.slice(i, i + 3).map(line => line.match(/[-+0-9.eE]+/g).map(Number));
      const a = tri[0], b = tri[1], c = tri[2];
      const u = [b[0]-a[0], b[1]-a[1], b[2]-a[2]], v = [c[0]-a[0], c[1]-a[1], c[2]-a[2]];
      const n = [u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0]];
      for (const point of tri) { out.push(...point); normals.push(...n); }
    }
    return out;
  };
  const load = async () => {
    const model = new URLSearchParams(location.search).get("model");
    if (!model || !model.startsWith("file:")) throw new Error("No local STL file was supplied.");
    const response = await fetch(model);
    if (!response.ok) throw new Error("The local STL model could not be read.");
    vertices = stlVertices(await response.arrayBuffer());
    if (!vertices.length) throw new Error("The STL contains no triangles.");
    let min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < vertices.length; i += 3) for (let axis = 0; axis < 3; axis += 1) {
      min[axis] = Math.min(min[axis], vertices[i+axis]); max[axis] = Math.max(max[axis], vertices[i+axis]);
    }
    const center = min.map((v, i) => (v + max[i]) / 2);
    const scale = 1.7 / Math.max(...max.map((v, i) => v - min[i]));
    vertices = vertices.map((v, i) => (v - center[i % 3]) * scale);
    positionBuffer = buffer(locations.position, vertices);
    normalBuffer = buffer(locations.normal, normals);
    const edges = [];
    for (let i = 0; i + 8 < vertices.length; i += 9) {
      for (const index of [0, 3, 3, 6, 6, 0]) {
        edges.push(vertices[i+index], vertices[i+index+1], vertices[i+index+2]);
      }
    }
    wireBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, wireBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(edges), gl.STATIC_DRAW);
    message.hidden = true;
    render();
  };
  const command = name => {
    if (name === "reset") { angleX = -0.5; angleY = 0.65; zoom = 2.8; }
    if (name === "zoom_in") zoom = Math.max(1.1, zoom - 0.2);
    if (name === "zoom_out") zoom = Math.min(20, zoom + 0.2);
    if (name === "rotate") angleY += 0.15;
    if (name === "wireframe") wireframe = !wireframe;
    if (name === "explode") exploded = !exploded;
    if (name === "grid") grid = !grid;
    if (name === "screenshot") {
      const link = document.createElement("a");
      link.download = "aura-cad-preview.png";
      link.href = canvas.toDataURL("image/png");
      link.click();
    }
    render();
  };
  document.querySelectorAll("button[data-command]").forEach(button =>
    button.addEventListener("click", () => command(button.dataset.command)));
  window.addEventListener("aura-cad-command", event => command(String(event.detail || "")));
  canvas.addEventListener("pointerdown", event => {
    dragging = true; lastX = event.clientX; lastY = event.clientY; canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointerup", () => { dragging = false; });
  canvas.addEventListener("pointermove", event => {
    if (!dragging) return;
    angleY += (event.clientX - lastX) * 0.008;
    angleX = Math.max(-1.45, Math.min(1.45, angleX + (event.clientY - lastY) * 0.008));
    lastX = event.clientX; lastY = event.clientY; render();
  });
  canvas.addEventListener("wheel", event => { zoom = Math.max(1.1, Math.min(20, zoom + event.deltaY * 0.002)); render(); });
  window.addEventListener("resize", render);
  load().catch(error => { message.textContent = error.message || "The model could not be displayed."; message.hidden = false; });
})();
