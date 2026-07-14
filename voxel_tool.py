bl_info = {
    "name": "Plane Sketch Tool",
    "author": "ChatGPT",
    "version": (0, 4, 4),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > PlaneSketch",
    "description": "GridArch/CreatePlane based rough sketch tool: place 1m orthogonal planes, slide edges, extrude orthogonal edges, then shrinkwrap at the end.",
    "category": "3D View",
}

import bpy
from mathutils import Vector, Matrix
from bpy_extras import view3d_utils
from bpy.types import PropertyGroup
from bpy.props import EnumProperty, FloatProperty, PointerProperty, BoolProperty


ADDON_PREFIX = "PST"
COLLECTION_NAME = "PlaneSketch_Objects"
MIN_SIZE = 0.001
PLACEMENT_GRID_SIZE = 1.0
TAG_KEY = "pst_tool"
TAG_VAL = "plane_sketch"
KIND_KEY = "pst_kind"
TARGET_KEY = "pst_target"
KIND_PLANE = "PLANE"


# ============================================================
# Basic utility
# ============================================================

def get_or_create_collection(context, name=COLLECTION_NAME):
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        context.scene.collection.children.link(col)
    return col


def mouse_ray(context, event):
    region = context.region
    rv3d = context.region_data
    mouse = (event.mouse_region_x, event.mouse_region_y)
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse)
    return origin, direction


def raycast_visible_mesh(context, event):
    origin, direction = mouse_ray(context, event)
    depsgraph = context.evaluated_depsgraph_get()
    hit, location, normal, face_index, obj, matrix = context.scene.ray_cast(depsgraph, origin, direction)
    if not hit or obj is None or obj.type != "MESH":
        return None
    return {
        "object": obj,
        "location": location,
        "normal": normal.normalized(),
        "face_index": face_index,
    }


def project_mouse_to_plane(context, event, plane_origin, plane_normal):
    ray_origin, ray_dir = mouse_ray(context, event)
    denom = ray_dir.dot(plane_normal)
    if abs(denom) < 1e-6:
        return None
    t = (plane_origin - ray_origin).dot(plane_normal) / denom
    if t < 0:
        return None
    return ray_origin + ray_dir * t


def passthrough_viewport_event(event):
    if event.type in {
        "MIDDLEMOUSE",
        "WHEELUPMOUSE",
        "WHEELDOWNMOUSE",
        "WHEELINMOUSE",
        "WHEELOUTMOUSE",
        "NDOF_MOTION",
        "NDOF_BUTTON_MENU",
        "NUMPAD_0",
        "NUMPAD_1",
        "NUMPAD_2",
        "NUMPAD_3",
        "NUMPAD_4",
        "NUMPAD_5",
        "NUMPAD_6",
        "NUMPAD_7",
        "NUMPAD_8",
        "NUMPAD_9",
        "HOME",
        "PERIOD",
        "NUMPAD_PERIOD",
        "Z",
    }:
        return True

    # Keep Alt/Ctrl combinations for Blender navigation or user shortcuts.
    if event.alt or event.ctrl:
        return True

    return False


def snap_value(value, step):
    if step <= 0:
        return float(value)
    return round(float(value) / step) * step


def tag_object(obj, kind=KIND_PLANE):
    obj[TAG_KEY] = TAG_VAL
    obj[KIND_KEY] = kind


def axis_normal_from_normal(normal):
    """Convert any surface normal to the nearest orthogonal world axis."""
    n = normal.normalized()
    candidates = [
        Vector((1, 0, 0)),
        Vector((-1, 0, 0)),
        Vector((0, 1, 0)),
        Vector((0, -1, 0)),
        Vector((0, 0, 1)),
        Vector((0, 0, -1)),
    ]
    return max(candidates, key=lambda axis: n.dot(axis))


def plane_matrix_from_axis_normal(location, normal):
    """Create a 90-degree orthogonal plane matrix whose local Z is the given axis normal."""
    z_axis = normal.normalized()

    if abs(z_axis.z) > 0.5:
        x_axis = Vector((1, 0, 0))
        y_axis = Vector((0, 1, 0)) if z_axis.z > 0 else Vector((0, -1, 0))
    elif abs(z_axis.y) > 0.5:
        x_axis = Vector((1, 0, 0))
        y_axis = Vector((0, 0, 1)) if z_axis.y > 0 else Vector((0, 0, -1))
    else:
        x_axis = Vector((0, 1, 0))
        y_axis = Vector((0, 0, 1)) if z_axis.x > 0 else Vector((0, 0, -1))

    # Rebuild Z from X/Y to keep the matrix orthogonal and consistent.
    z_axis = x_axis.cross(y_axis).normalized()
    if z_axis.dot(normal) < 0:
        y_axis.negate()
        z_axis = x_axis.cross(y_axis).normalized()

    return Matrix((
        (x_axis.x, y_axis.x, z_axis.x, location.x),
        (x_axis.y, y_axis.y, z_axis.y, location.y),
        (x_axis.z, y_axis.z, z_axis.z, location.z),
        (0.0,      0.0,      0.0,      1.0),
    ))


def is_plane_sketch_object(obj):
    return obj is not None and obj.type == "MESH" and obj.get(TAG_KEY) == TAG_VAL


def active_plane_object(context):
    obj = context.object
    if is_plane_sketch_object(obj):
        return obj
    return None


def get_local_axes(obj):
    m = obj.matrix_world.to_3x3()
    return {
        "x": (m @ Vector((1, 0, 0))).normalized(),
        "y": (m @ Vector((0, 1, 0))).normalized(),
        "z": (m @ Vector((0, 0, 1))).normalized(),
    }


# ============================================================
# Settings
# ============================================================

class PST_Settings(PropertyGroup):
    snap_size: EnumProperty(
        name="Snap Size",
        items=[
            ("0.25", "0.25 m", "Quarter meter snap"),
            ("0.5", "0.5 m", "Half meter snap"),
            ("1.0", "1.0 m", "One meter snap"),
        ],
        default="1.0",
    )

    ground_level: FloatProperty(
        name="Ground Level",
        default=0.0,
        description="Z level for horizontal placement, or Y level for vertical XZ placement",
    )

    shrinkwrap_offset: FloatProperty(
        name="Shrinkwrap Offset",
        default=0.0,
        soft_min=-1.0,
        soft_max=1.0,
    )

    wire_display: BoolProperty(
        name="Wire Display",
        default=False,
    )


# ============================================================
# Plane mesh core
# ============================================================

def normalize_plane_bounds(bounds):
    result = dict(bounds)
    if result["max_u"] - result["min_u"] < MIN_SIZE:
        mid = (result["min_u"] + result["max_u"]) * 0.5
        result["min_u"] = mid - MIN_SIZE * 0.5
        result["max_u"] = mid + MIN_SIZE * 0.5
    if result["max_v"] - result["min_v"] < MIN_SIZE:
        mid = (result["min_v"] + result["max_v"]) * 0.5
        result["min_v"] = mid - MIN_SIZE * 0.5
        result["max_v"] = mid + MIN_SIZE * 0.5
    return result


def get_plane_bounds(obj):
    if obj is None or obj.type != "MESH" or len(obj.data.vertices) < 4:
        return None
    xs = [v.co.x for v in obj.data.vertices]
    ys = [v.co.y for v in obj.data.vertices]
    return normalize_plane_bounds({
        "min_u": min(xs),
        "max_u": max(xs),
        "min_v": min(ys),
        "max_v": max(ys),
    })


def update_plane_mesh_from_bounds(obj, bounds):
    bounds = normalize_plane_bounds(bounds)
    min_u = bounds["min_u"]
    max_u = bounds["max_u"]
    min_v = bounds["min_v"]
    max_v = bounds["max_v"]

    mesh = obj.data
    mesh.clear_geometry()
    mesh.from_pydata(
        [
            (min_u, min_v, 0.0),
            (max_u, min_v, 0.0),
            (max_u, max_v, 0.0),
            (min_u, max_v, 0.0),
        ],
        [],
        [(0, 1, 2, 3)],
    )
    mesh.update()

    obj["pst_min_u"] = float(min_u)
    obj["pst_max_u"] = float(max_u)
    obj["pst_min_v"] = float(min_v)
    obj["pst_max_v"] = float(max_v)


def create_plane_object(context, name, bounds, matrix, target_obj=None):
    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    obj = bpy.data.objects.new(name, mesh)
    obj.matrix_world = matrix.copy()
    tag_object(obj, KIND_PLANE)

    if target_obj is not None:
        obj[TARGET_KEY] = target_obj.name

    update_plane_mesh_from_bounds(obj, bounds)

    s = context.scene.plane_sketch
    if bool(s.wire_display):
        obj.display_type = "WIRE"
        obj.show_in_front = True

    col = get_or_create_collection(context)
    col.objects.link(obj)

    bpy.ops.object.select_all(action="DESELECT")
    context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj


def make_unit_plane_bounds(unit=1.0):
    h = 0.5
    return {
        "min_u": -h,
        "max_u": h,
        "min_v": -h,
        "max_v": h,
    }


def plane_matrix_for_mode(location, vertical=False):
    if vertical:
        # Local XY plane becomes world XZ plane. Local Z normal points world -Y.
        rot = Matrix.Rotation(1.5707963267948966, 4, "X")
        matrix = rot
        matrix.translation = location
        return matrix

    matrix = Matrix.Identity(4)
    matrix.translation = location
    return matrix


def placement_from_event(context, event):
    """Return (location, matrix) for 1m orthogonal placement.

    Empty-space clicks always create an XY plane on ground_level.
    Object hits use the clicked face normal, rounded to the nearest world X/Y/Z axis.
    The placement location is always snapped to a 1m grid so planes remain orthogonal and grid-aligned.
    """
    level = float(context.scene.plane_sketch.ground_level)
    grid = PLACEMENT_GRID_SIZE

    hit = raycast_visible_mesh(context, event)
    if hit is not None and not is_plane_sketch_object(hit["object"]):
        axis_normal = axis_normal_from_normal(hit["normal"])
        hit_loc = hit["location"]

        # Snap all coordinates to the global 1m grid.
        # This deliberately ignores Snap Size; Snap Size is for editing, not placement.
        loc = Vector((
            snap_value(hit_loc.x, grid),
            snap_value(hit_loc.y, grid),
            snap_value(hit_loc.z, grid),
        ))
        matrix = plane_matrix_from_axis_normal(loc, axis_normal)
        return loc, matrix

    # Empty-space placement: always horizontal XY plane on ground_level.
    origin = Vector((0.0, 0.0, level))
    normal = Vector((0.0, 0.0, 1.0))
    projected = project_mouse_to_plane(context, event, origin, normal)
    if projected is None:
        return None, None

    loc = Vector((
        snap_value(projected.x, grid),
        snap_value(projected.y, grid),
        snap_value(level, grid),
    ))
    matrix = plane_matrix_from_axis_normal(loc, normal)
    return loc, matrix


def find_plane_at_location(location, unit, matrix=None):
    tol = max(0.001, unit * 0.1)
    for obj in bpy.data.objects:
        if not is_plane_sketch_object(obj):
            continue
        if (obj.location - location).length > tol:
            continue
        if matrix is not None:
            n0 = (obj.matrix_world.to_3x3() @ Vector((0, 0, 1))).normalized()
            n1 = (matrix.to_3x3() @ Vector((0, 0, 1))).normalized()
            if abs(n0.dot(n1)) < 0.999:
                continue
        return obj
    return None


def plane_under_mouse(context, event):
    hit = raycast_visible_mesh(context, event)
    if hit is not None and is_plane_sketch_object(hit["object"]):
        return hit["object"]
    return active_plane_object(context)


def join_plane_objects(context, objects):
    valid = []
    seen = set()
    for obj in objects:
        if obj and obj.name in bpy.data.objects and is_plane_sketch_object(obj) and obj.name not in seen:
            valid.append(obj)
            seen.add(obj.name)

    if len(valid) <= 1:
        return valid[0] if valid else None

    if context.object and context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    bpy.ops.object.select_all(action="DESELECT")
    for obj in valid:
        obj.select_set(True)

    context.view_layer.objects.active = valid[0]
    bpy.ops.object.join()
    joined = context.view_layer.objects.active
    tag_object(joined, KIND_PLANE)
    joined.name = f"{ADDON_PREFIX}_Plane_Joined"
    return joined


def delete_plane_at_location(location, unit):
    obj = find_plane_at_location(location, unit)
    if obj is not None:
        bpy.data.objects.remove(obj, do_unlink=True)
        return True
    return False


# ============================================================
# Place planes: GridArch-like LMB/RMB sketching
# ============================================================

class PST_OT_place_plane_grid(bpy.types.Operator):
    bl_idname = "planesketch.place_plane"
    bl_label = "Place Plane"
    bl_description = "LMB place plane, RMB delete. Normal placement is horizontal XY. Shift placement is vertical XZ. ESC exits."
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        if context.area.type != "VIEW_3D":
            self.report({"WARNING"}, "3D View only")
            return {"CANCELLED"}

        self.is_lmb_drag = False
        self.is_rmb_drag = False
        self.last_location = None
        self.created_planes = []
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set("PlaneSketch: LMB place 1m plane / RMB delete / object hit snaps to nearest axis / ESC exit")
        return {"RUNNING_MODAL"}

    def _place_at_event(self, context, event):
        s = context.scene.plane_sketch
        unit = PLACEMENT_GRID_SIZE
        loc, matrix = placement_from_event(context, event)
        if loc is None or matrix is None:
            return

        if self.last_location is not None and (loc - self.last_location).length < unit * 0.5:
            return

        if find_plane_at_location(loc, unit, matrix=matrix):
            self.last_location = loc
            return

        new_obj = create_plane_object(
            context,
            f"{ADDON_PREFIX}_Plane",
            make_unit_plane_bounds(),
            matrix,
        )
        self.created_planes.append(new_obj)
        self.last_location = loc

    def _delete_at_event(self, context, event):
        s = context.scene.plane_sketch
        unit = PLACEMENT_GRID_SIZE
        loc, matrix = placement_from_event(context, event)
        if loc is None:
            return
        delete_plane_at_location(loc, unit)
        self.last_location = loc

    def modal(self, context, event):
        if event.type == "ESC":
            joined = join_plane_objects(context, self.created_planes)
            if joined:
                try:
                    bpy.ops.object.select_all(action="DESELECT")
                    joined.select_set(True)
                    context.view_layer.objects.active = joined
                except Exception:
                    pass
            context.area.header_text_set(None)
            return {"FINISHED"}

        if passthrough_viewport_event(event):
            return {"PASS_THROUGH"}

        if event.type == "LEFTMOUSE":
            if event.value == "PRESS":
                self.is_lmb_drag = True
                self.last_location = None
                self._place_at_event(context, event)
                return {"RUNNING_MODAL"}
            if event.value == "RELEASE":
                self.is_lmb_drag = False
                self.last_location = None
                return {"RUNNING_MODAL"}

        if event.type == "RIGHTMOUSE":
            if event.value == "PRESS":
                self.is_rmb_drag = True
                self.last_location = None
                self._delete_at_event(context, event)
                return {"RUNNING_MODAL"}
            if event.value == "RELEASE":
                self.is_rmb_drag = False
                self.last_location = None
                return {"RUNNING_MODAL"}

        if event.type == "MOUSEMOVE":
            if self.is_lmb_drag:
                self._place_at_event(context, event)
                return {"RUNNING_MODAL"}
            if self.is_rmb_drag:
                self._delete_at_event(context, event)
                return {"RUNNING_MODAL"}

        return {"RUNNING_MODAL"}


# ============================================================
# Edit plane: LMB slides edge, RMB extrudes orthogonal edge
# ============================================================

def nearest_plane_edge(local_point, bounds):
    distances = {
        "min_u": abs(local_point.x - bounds["min_u"]),
        "max_u": abs(local_point.x - bounds["max_u"]),
        "min_v": abs(local_point.y - bounds["min_v"]),
        "max_v": abs(local_point.y - bounds["max_v"]),
    }
    return min(distances, key=distances.get)


def apply_edge_slide(bounds, edge_key, local_point, snap_step=0.0):
    result = bounds.copy()

    if edge_key == "min_u":
        v = snap_value(local_point.x, snap_step) if snap_step > 0 else local_point.x
        result["min_u"] = min(v, bounds["max_u"] - MIN_SIZE)
    elif edge_key == "max_u":
        v = snap_value(local_point.x, snap_step) if snap_step > 0 else local_point.x
        result["max_u"] = max(v, bounds["min_u"] + MIN_SIZE)
    elif edge_key == "min_v":
        v = snap_value(local_point.y, snap_step) if snap_step > 0 else local_point.y
        result["min_v"] = min(v, bounds["max_v"] - MIN_SIZE)
    elif edge_key == "max_v":
        v = snap_value(local_point.y, snap_step) if snap_step > 0 else local_point.y
        result["max_v"] = max(v, bounds["min_v"] + MIN_SIZE)

    return normalize_plane_bounds(result)


class PST_OT_edit_plane_size(bpy.types.Operator):
    bl_idname = "planesketch.edit_plane"
    bl_label = "Edit Plane"
    bl_description = "LMB slides nearest edge. RMB extrudes a perpendicular plane from nearest edge. ESC exits."
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        if context.area.type != "VIEW_3D":
            self.report({"WARNING"}, "3D View only")
            return {"CANCELLED"}

        obj = active_plane_object(context)
        if obj is None:
            self.report({"WARNING"}, "Select a PlaneSketch plane first")
            return {"CANCELLED"}

        bounds = get_plane_bounds(obj)
        if bounds is None:
            self.report({"WARNING"}, "Selected object is not a valid PlaneSketch plane")
            return {"CANCELLED"}

        self.plane_obj = obj
        self.dragging = False
        self.mode = None
        self.active_edge = None
        self.start_bounds = bounds.copy()
        self.preview_obj = None
        self.plane_origin = obj.matrix_world.translation.copy()
        self.plane_normal = (obj.matrix_world.to_3x3() @ Vector((0, 0, 1))).normalized()

        context.window_manager.modal_handler_add(self)
        context.area.header_text_set("PlaneSketch Edit: LMB edge slide / RMB orthogonal extrude / ESC exit")
        return {"RUNNING_MODAL"}

    def _create_orthogonal_matrix(self, edge_key):
        src = self.plane_obj
        axes = get_local_axes(src)
        bounds = self.start_bounds

        if edge_key in {"min_u", "max_u"}:
            u = bounds[edge_key]
            center_v = (bounds["min_v"] + bounds["max_v"]) * 0.5
            edge_center = src.matrix_world @ Vector((u, center_v, 0.0))
            x_axis = axes["y"]
            y_axis = axes["z"]
            z_axis = x_axis.cross(y_axis).normalized()
        else:
            v = bounds[edge_key]
            center_u = (bounds["min_u"] + bounds["max_u"]) * 0.5
            edge_center = src.matrix_world @ Vector((center_u, v, 0.0))
            x_axis = axes["x"]
            y_axis = axes["z"]
            z_axis = x_axis.cross(y_axis).normalized()

        return Matrix((
            (x_axis.x, y_axis.x, z_axis.x, edge_center.x),
            (x_axis.y, y_axis.y, z_axis.y, edge_center.y),
            (x_axis.z, y_axis.z, z_axis.z, edge_center.z),
            (0.0,      0.0,      0.0,      1.0),
        ))

    def _initial_orthogonal_bounds(self, edge_key):
        b = self.start_bounds
        length = (b["max_v"] - b["min_v"]) if edge_key in {"min_u", "max_u"} else (b["max_u"] - b["min_u"])
        half = length * 0.5
        return {"min_u": -half, "max_u": half, "min_v": 0.0, "max_v": MIN_SIZE}

    def modal(self, context, event):
        if event.type == "ESC":
            if self.preview_obj is not None and self.preview_obj.name in bpy.data.objects and self.dragging:
                bpy.data.objects.remove(self.preview_obj, do_unlink=True)
            context.area.header_text_set(None)
            return {"CANCELLED" if self.dragging else "FINISHED"}

        if passthrough_viewport_event(event):
            return {"PASS_THROUGH"}

        if not self.dragging and event.type in {"LEFTMOUSE", "RIGHTMOUSE"} and event.value == "PRESS":
            target_obj = plane_under_mouse(context, event)
            if target_obj is None:
                return {"RUNNING_MODAL"}

            self.plane_obj = target_obj
            self.plane_origin = self.plane_obj.matrix_world.translation.copy()
            self.plane_normal = (self.plane_obj.matrix_world.to_3x3() @ Vector((0, 0, 1))).normalized()

            current_world = project_mouse_to_plane(context, event, self.plane_origin, self.plane_normal)
            if current_world is None:
                return {"RUNNING_MODAL"}

            current_local = self.plane_obj.matrix_world.inverted() @ current_world
            bounds = get_plane_bounds(self.plane_obj)
            if bounds is None:
                context.area.header_text_set(None)
                return {"CANCELLED"}

            self.start_bounds = bounds.copy()
            self.active_edge = nearest_plane_edge(current_local, bounds)
            self.dragging = True

            if event.type == "LEFTMOUSE":
                self.mode = "SLIDE"
                context.area.header_text_set(
                    f"PlaneSketch Edit: sliding {self.active_edge} / release to continue / ESC exit"
                )
            else:
                self.mode = "ORTHO_EXTRUDE"
                matrix = self._create_orthogonal_matrix(self.active_edge)
                bounds = self._initial_orthogonal_bounds(self.active_edge)
                self.preview_obj = create_plane_object(
                    context,
                    f"{ADDON_PREFIX}_OrthoPlane",
                    bounds,
                    matrix,
                )
                context.area.header_text_set(
                    f"PlaneSketch Edit: orthogonal extrude from {self.active_edge} / release to continue / ESC exit"
                )
            return {"RUNNING_MODAL"}

        if event.type == "MOUSEMOVE" and self.dragging:
            s = context.scene.plane_sketch
            snap_step = float(s.snap_size)

            if self.mode == "SLIDE":
                current_world = project_mouse_to_plane(context, event, self.plane_origin, self.plane_normal)
                if current_world is not None:
                    current_local = self.plane_obj.matrix_world.inverted() @ current_world
                    bounds = apply_edge_slide(self.start_bounds, self.active_edge, current_local, snap_step=snap_step)
                    update_plane_mesh_from_bounds(self.plane_obj, bounds)
                return {"RUNNING_MODAL"}

            if self.mode == "ORTHO_EXTRUDE" and self.preview_obj is not None:
                preview_normal = (self.preview_obj.matrix_world.to_3x3() @ Vector((0, 0, 1))).normalized()
                preview_origin = self.preview_obj.matrix_world.translation.copy()
                current_world = project_mouse_to_plane(context, event, preview_origin, preview_normal)
                if current_world is not None:
                    local = self.preview_obj.matrix_world.inverted() @ current_world
                    v = snap_value(local.y, snap_step)
                    bounds = get_plane_bounds(self.preview_obj)
                    if bounds is not None:
                        if v >= 0:
                            bounds["min_v"] = 0.0
                            bounds["max_v"] = max(v, MIN_SIZE)
                        else:
                            bounds["min_v"] = min(v, -MIN_SIZE)
                            bounds["max_v"] = 0.0
                        update_plane_mesh_from_bounds(self.preview_obj, bounds)
                return {"RUNNING_MODAL"}

        if event.type in {"LEFTMOUSE", "RIGHTMOUSE"} and event.value == "RELEASE" and self.dragging:
            self.dragging = False
            self.mode = None
            self.active_edge = None
            self.preview_obj = None
            self.start_bounds = get_plane_bounds(self.plane_obj) or self.start_bounds
            try:
                bpy.ops.object.select_all(action="DESELECT")
                self.plane_obj.select_set(True)
                context.view_layer.objects.active = self.plane_obj
            except Exception:
                pass
            context.area.header_text_set("PlaneSketch Edit: LMB edge slide / RMB orthogonal extrude / ESC exit")
            return {"RUNNING_MODAL"}

        return {"RUNNING_MODAL"}


# ============================================================
# Shrinkwrap last-step helper
# ============================================================

class PST_OT_set_target_from_selection(bpy.types.Operator):
    bl_idname = "planesketch.set_target_from_selection"
    bl_label = "Set Target From Selection"
    bl_description = "Set active PlaneSketch object shrinkwrap target from another selected mesh"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        active = context.view_layer.objects.active
        if not is_plane_sketch_object(active):
            self.report({"WARNING"}, "Active object must be a PlaneSketch object")
            return {"CANCELLED"}

        candidates = [o for o in context.selected_objects if o != active and o.type == "MESH" and not is_plane_sketch_object(o)]
        if not candidates:
            self.report({"WARNING"}, "Select a source mesh together with the active PlaneSketch object")
            return {"CANCELLED"}

        target = candidates[0]
        active[TARGET_KEY] = target.name
        self.report({"INFO"}, f"Target set: {target.name}")
        return {"FINISHED"}


class PST_OT_add_shrinkwrap_to_target(bpy.types.Operator):
    bl_idname = "planesketch.add_shrinkwrap"
    bl_label = "Shrinkwrap To Target"
    bl_description = "Add or update Shrinkwrap modifier using the stored target object. Intended as the final step."
    bl_options = {"REGISTER", "UNDO"}

    apply_modifier: BoolProperty(
        name="Apply Modifier",
        default=False,
    )

    def execute(self, context):
        s = context.scene.plane_sketch
        obj = active_plane_object(context)
        if obj is None:
            self.report({"WARNING"}, "Select a PlaneSketch object first")
            return {"CANCELLED"}

        target_name = obj.get(TARGET_KEY)
        target_obj = bpy.data.objects.get(target_name) if target_name else None
        if target_obj is None:
            self.report({"WARNING"}, "Plane has no stored target object. Use Set Target From Selection first.")
            return {"CANCELLED"}

        mod = obj.modifiers.get("PST_Shrinkwrap")
        if mod is None:
            mod = obj.modifiers.new(name="PST_Shrinkwrap", type="SHRINKWRAP")

        mod.target = target_obj
        mod.offset = float(s.shrinkwrap_offset)
        mod.wrap_method = "NEAREST_SURFACEPOINT"

        if self.apply_modifier:
            context.view_layer.objects.active = obj
            try:
                bpy.ops.object.modifier_apply(modifier=mod.name)
            except Exception as exc:
                self.report({"WARNING"}, f"Could not apply shrinkwrap: {exc}")
                return {"CANCELLED"}

        self.report({"INFO"}, f"Shrinkwrap target: {target_obj.name}, offset: {s.shrinkwrap_offset:g}m")
        return {"FINISHED"}


# ============================================================
# UI Panel
# ============================================================

class PST_PT_panel(bpy.types.Panel):
    bl_label = "PlaneSketch"
    bl_idname = "PST_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PlaneSketch"

    def draw(self, context):
        layout = self.layout
        s = context.scene.plane_sketch

        col = layout.column(align=True)
        col.label(text="Edit Snap Size")
        col.prop(s, "snap_size")
        col.prop(s, "ground_level")
        col.prop(s, "wire_display")

        layout.separator()

        col = layout.column(align=True)
        col.label(text="Place")
        col.operator("planesketch.place_plane", text="Place Plane")

        layout.separator()

        col = layout.column(align=True)
        col.label(text="Edit Size")
        col.operator("planesketch.edit_plane", text="Edit Plane")


        layout.separator()

        col = layout.column(align=True)
        col.label(text="Shrinkwrap Last")
        col.prop(s, "shrinkwrap_offset")
        col.operator("planesketch.set_target_from_selection", text="Set Target From Selection")
        col.operator("planesketch.add_shrinkwrap", text="Shrinkwrap To Target")

        obj = context.object
        if is_plane_sketch_object(obj):
            bounds = get_plane_bounds(obj)
            layout.separator()
            layout.label(text="Selected PlaneSketch")
            if bounds:
                su = bounds["max_u"] - bounds["min_u"]
                sv = bounds["max_v"] - bounds["min_v"]
                layout.label(text=f"Size: {su:.3f} x {sv:.3f} m")
            target_name = obj.get(TARGET_KEY)
            if target_name:
                layout.label(text=f"Target: {target_name}")


classes = (
    PST_Settings,
    PST_OT_place_plane_grid,
    PST_OT_edit_plane_size,
    PST_OT_set_target_from_selection,
    PST_OT_add_shrinkwrap_to_target,
    PST_PT_panel,
)


LEGACY_CLASS_NAMES = (
    "BPT_OT_create_bbox_proxy_from_selection",
    "BPT_OT_click_bbox_proxy",
    "BPT_OT_create_surface_proxy",
    "BPT_OT_edit_proxy_box",
    "BPT_OT_add_shrinkwrap_to_target",
    "BPT_PT_panel",
    "SRP_OT_place_plane_on_face",
    "SRP_OT_create_fitted_plane",
    "SRP_OT_drag_rectangle_plane",
    "SRP_OT_edit_rectangle_plane",
    "SRP_PT_panel",
    "PST_OT_grid_place",
    "PST_OT_create_bbox_proxy_from_selection",
    "PST_OT_click_bbox_proxy",
    "PST_OT_edit_proxy_box",
    "PST_OT_create_collision_bbox",
    "PST_OT_extrude_plane_edge",
)

LEGACY_CATEGORIES = (
    "BBoxProxy",
    "SurfacePlane",
    "CreatePlane",
    "ProxySketch",
)


def unregister_legacy_classes():
    current_class_names = {cls.__name__ for cls in classes}
    for type_name in dir(bpy.types):
        if type_name in current_class_names:
            continue
        cls = getattr(bpy.types, type_name, None)
        if cls is None:
            continue
        bl_category = getattr(cls, "bl_category", "")
        if type_name in LEGACY_CLASS_NAMES or bl_category in LEGACY_CATEGORIES:
            try:
                bpy.utils.unregister_class(cls)
            except RuntimeError:
                pass


def register():
    unregister_legacy_classes()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.plane_sketch = PointerProperty(type=PST_Settings)


def unregister():
    if hasattr(bpy.types.Scene, "plane_sketch"):
        del bpy.types.Scene.plane_sketch
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
    unregister_legacy_classes()


if __name__ == "__main__":
    register()
