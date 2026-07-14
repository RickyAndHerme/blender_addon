bl_info = {
    "name": "EditCube",
    "author": "ChatGPT",
    "version": (0, 8, 0),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > EditCube",
    "description": "Single-tool mirrored cube blockout: 1m create snap, 0.5m face edit snap.",
    "category": "3D View",
}

import bpy
from mathutils import Matrix, Vector
from bpy_extras import view3d_utils


# ============================================================
# Constants
# ============================================================

ADDON_PREFIX = "GA"
COLLECTION_NAME = "EditCube_Objects"
TAG_KEY = "editcube_tool"
KIND_KEY = "editcube_kind"
KIND_CUBE = "CUBE"
MIRROR_MOD_NAME = "ECB_Mirror_XY"
LEGACY_MIRROR_MOD_NAMES = ("ECB_Mirror_X", "ECB_Mirror_Y")
SOLIDIFY_MOD_NAME = "ECB_Solidify_1m"

DEFAULT_CUBE_SIZE = 1.0
DEFAULT_HALF_SIZE = DEFAULT_CUBE_SIZE * 0.5
CREATE_SNAP = 1.0
EDIT_SNAP = 0.5
MIN_SIZE = 0.001
DRAG_SCALE = 0.02

VIEW_PASSTHROUGH_EVENTS = {
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
}

AXIS_VECTORS = {
    "x": Vector((1.0, 0.0, 0.0)),
    "y": Vector((0.0, 1.0, 0.0)),
    "z": Vector((0.0, 0.0, 1.0)),
}


# ============================================================
# Object helpers
# ============================================================

def get_or_create_collection(context):
    collection = bpy.data.collections.get(COLLECTION_NAME)
    if collection is not None:
        return collection
    return context.scene.collection


def tag_cube(obj):
    obj[TAG_KEY] = True
    obj[KIND_KEY] = KIND_CUBE
    if getattr(obj, "data", None) is not None:
        obj.data[TAG_KEY] = True
        obj.data[KIND_KEY] = KIND_CUBE


def is_editcube_object(obj):
    return obj is not None and obj.type == "MESH" and obj.get(TAG_KEY) is True


def active_cube(context):
    obj = context.object
    return obj if is_editcube_object(obj) else None


def select_only(context, obj):
    bpy.ops.object.select_all(action="DESELECT")
    if obj and obj.name in bpy.data.objects:
        obj.select_set(True)
        context.view_layer.objects.active = obj


# ============================================================
# Viewport / raycast helpers
# ============================================================

def mouse_ray(context, event):
    coord = (event.mouse_region_x, event.mouse_region_y)
    origin = view3d_utils.region_2d_to_origin_3d(context.region, context.region_data, coord)
    direction = view3d_utils.region_2d_to_vector_3d(context.region, context.region_data, coord)
    return origin, direction.normalized()


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


def raycast_object(context, event, obj):
    """Raycast against one object only. Used so overlapping cubes do not steal face edits."""
    if obj is None or obj.type != "MESH":
        return None

    origin, direction = mouse_ray(context, event)
    depsgraph = context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)
    inv = eval_obj.matrix_world.inverted()
    local_origin = inv @ origin
    local_direction = (inv.to_3x3() @ direction).normalized()

    hit, local_location, local_normal, face_index = eval_obj.ray_cast(local_origin, local_direction)
    if not hit:
        return None

    return {
        "object": obj,
        "location": eval_obj.matrix_world @ local_location,
        "normal": (eval_obj.matrix_world.to_3x3() @ local_normal).normalized(),
        "face_index": face_index,
    }


def project_mouse_to_world_xy(context, event, z=0.0):
    origin, direction = mouse_ray(context, event)
    if abs(direction.z) < 1e-8:
        return None

    t = (z - origin.z) / direction.z
    return origin + direction * t if t >= 0.0 else None


def screen_axis_for_world_axis(context, world_origin, world_axis):
    p0 = view3d_utils.location_3d_to_region_2d(context.region, context.region_data, world_origin)
    p1 = view3d_utils.location_3d_to_region_2d(
        context.region,
        context.region_data,
        world_origin + world_axis.normalized(),
    )

    if p0 is None or p1 is None:
        return None

    axis = Vector((p1.x - p0.x, p1.y - p0.y))
    return axis.normalized() if axis.length >= 1e-6 else None


def is_view_passthrough(event, allow_ctrl=False):
    if event.type in VIEW_PASSTHROUGH_EVENTS:
        return True
    if event.alt or event.shift:
        return True
    if event.ctrl and not allow_ctrl:
        return True
    return False


# ============================================================
# Bounds / mesh helpers
# ============================================================

def snap_value(value, step):
    return round(float(value) / step) * step


def snap_vector(v, step):
    return Vector((snap_value(v.x, step), snap_value(v.y, step), snap_value(v.z, step)))


def cube_prop_key(key):
    return f"editcube_{key}"


def read_cube_bounds_properties(id_block):
    if id_block is None:
        return None

    keys = ("min_x", "max_x", "min_y", "max_y", "min_z", "max_z")
    if not all(cube_prop_key(key) in id_block for key in keys):
        return None

    return normalize_bounds({key: float(id_block[cube_prop_key(key)]) for key in keys})


def write_cube_bounds_properties(obj, bounds):
    for id_block in (getattr(obj, "data", None), obj):
        if id_block is None:
            continue
        for key, value in bounds.items():
            id_block[cube_prop_key(key)] = float(value)


def default_cube_bounds():
    return {
        "min_x": -DEFAULT_HALF_SIZE,
        "max_x": DEFAULT_HALF_SIZE,
        "min_y": -DEFAULT_HALF_SIZE,
        "max_y": DEFAULT_HALF_SIZE,
        "min_z": 0.0,
        "max_z": DEFAULT_CUBE_SIZE,
    }


def normalize_bounds(bounds):
    result = dict(bounds)
    for axis in "xyz":
        min_key = f"min_{axis}"
        max_key = f"max_{axis}"
        if result[max_key] - result[min_key] < MIN_SIZE:
            mid = (result[min_key] + result[max_key]) * 0.5
            result[min_key] = mid - MIN_SIZE * 0.5
            result[max_key] = mid + MIN_SIZE * 0.5
    return result


def bounds_center(bounds):
    return Vector((
        (bounds["min_x"] + bounds["max_x"]) * 0.5,
        (bounds["min_y"] + bounds["max_y"]) * 0.5,
        (bounds["min_z"] + bounds["max_z"]) * 0.5,
    ))


def cube_bounds(obj):
    if obj is None or obj.type != "MESH":
        return None

    stored = read_cube_bounds_properties(getattr(obj, "data", None)) or read_cube_bounds_properties(obj)
    if stored is not None:
        return stored

    if len(obj.data.vertices) < 4:
        return None

    coords = [v.co for v in obj.data.vertices]
    x_extent = max(abs(v.x) for v in coords)
    y_extent = max(abs(v.y) for v in coords)
    return normalize_bounds({
        "min_x": -x_extent,
        "max_x": x_extent,
        "min_y": -y_extent,
        "max_y": y_extent,
        "min_z": 0.0,
        "max_z": DEFAULT_CUBE_SIZE,
    })


def quarter_plane_geometry(x_extent=DEFAULT_HALF_SIZE, y_extent=DEFAULT_HALF_SIZE):
    x = max(float(x_extent), EDIT_SNAP * 0.5)
    y = max(float(y_extent), EDIT_SNAP * 0.5)
    verts = [
        (0.0, 0.0, 0.0),
        (x, 0.0, 0.0),
        (x, y, 0.0),
        (0.0, y, 0.0),
    ]
    faces = [(0, 1, 2, 3)]
    return verts, faces


def ensure_editcube_modifiers(obj):
    mirror = obj.modifiers.get(MIRROR_MOD_NAME)
    if mirror is None:
        for legacy_name in LEGACY_MIRROR_MOD_NAMES:
            legacy = obj.modifiers.get(legacy_name)
            if legacy is not None and legacy.type == "MIRROR":
                mirror = legacy
                mirror.name = MIRROR_MOD_NAME
                break
    if mirror is None or mirror.type != "MIRROR":
        mirror = obj.modifiers.new(MIRROR_MOD_NAME, "MIRROR")
    mirror.use_axis = (True, True, False)
    mirror.use_clip = True
    mirror.use_mirror_merge = True
    mirror.merge_threshold = 0.001

    for legacy_name in LEGACY_MIRROR_MOD_NAMES:
        legacy = obj.modifiers.get(legacy_name)
        if legacy is not None:
            obj.modifiers.remove(legacy)

    solidify = obj.modifiers.get(SOLIDIFY_MOD_NAME)
    if solidify is None or solidify.type != "SOLIDIFY":
        solidify = obj.modifiers.new(SOLIDIFY_MOD_NAME, "SOLIDIFY")
    solidify.thickness = DEFAULT_CUBE_SIZE
    solidify.offset = 1.0


def ensure_quarter_plane_mesh(obj, x_extent=DEFAULT_HALF_SIZE, y_extent=DEFAULT_HALF_SIZE):
    if obj is None or obj.type != "MESH":
        return False
    verts, faces = quarter_plane_geometry(x_extent, y_extent)
    obj.data.clear_geometry()
    obj.data.from_pydata(verts, [], faces)
    obj.data.update()
    ensure_editcube_modifiers(obj)
    return True


def update_cube_mesh(obj, bounds):
    bounds = normalize_bounds(bounds)
    x_extent = max(abs(bounds["min_x"]), abs(bounds["max_x"]), EDIT_SNAP * 0.5)
    y_extent = max(abs(bounds["min_y"]), abs(bounds["max_y"]), EDIT_SNAP * 0.5)
    ensure_quarter_plane_mesh(obj, x_extent, y_extent)
    final_bounds = {
        "min_x": -x_extent,
        "max_x": x_extent,
        "min_y": -y_extent,
        "max_y": y_extent,
        "min_z": 0.0,
        "max_z": DEFAULT_CUBE_SIZE,
    }
    write_cube_bounds_properties(obj, final_bounds)
    return True


def bounds_from_create_drag(obj, start_world, current_world):
    start_local = obj.matrix_world.inverted() @ start_world
    current_local = obj.matrix_world.inverted() @ current_world
    x_extent = max(abs(start_local.x), abs(current_local.x), DEFAULT_HALF_SIZE)
    y_extent = max(abs(start_local.y), abs(current_local.y), DEFAULT_HALF_SIZE)

    return normalize_bounds({
        "min_x": -snap_value(x_extent, EDIT_SNAP),
        "max_x": snap_value(x_extent, EDIT_SNAP),
        "min_y": -snap_value(y_extent, EDIT_SNAP),
        "max_y": snap_value(y_extent, EDIT_SNAP),
        "min_z": 0.0,
        "max_z": DEFAULT_CUBE_SIZE,
    })


# ============================================================
# Cube creation / deletion / origin
# ============================================================

def cube_location_from_mouse(context, event):
    hit = raycast_visible_mesh(context, event)
    if hit is not None:
        return snap_vector(hit["location"], CREATE_SNAP)

    projected = project_mouse_to_world_xy(context, event, z=0.0)
    return snap_vector(projected, CREATE_SNAP) if projected is not None else None


def create_drag_location(context, event, z):
    projected = project_mouse_to_world_xy(context, event, z=z)
    return snap_vector(projected, EDIT_SNAP) if projected is not None else None


def create_cube(context, location):
    mesh = bpy.data.meshes.new(f"{ADDON_PREFIX}_ECubeMesh")
    obj = bpy.data.objects.new(f"{ADDON_PREFIX}_ECube", mesh)
    obj.matrix_world = Matrix.Translation(Vector((location.x, location.y, location.z)))
    tag_cube(obj)
    update_cube_mesh(obj, default_cube_bounds())

    # Link into the master scene collection; do not auto-create a dedicated collection.
    context.scene.collection.objects.link(obj)
    select_only(context, obj)
    return obj


def cube_from_mouse(context, event):
    hit = raycast_visible_mesh(context, event)
    if hit is None or not is_editcube_object(hit["object"]):
        return None
    return hit["object"]


def select_cube_under_mouse(context, event):
    obj = cube_from_mouse(context, event)
    if obj is None:
        return False
    select_only(context, obj)
    return True


def delete_cube_under_mouse(context, event):
    obj = cube_from_mouse(context, event)
    if obj is None:
        return False
    bpy.data.objects.remove(obj, do_unlink=True)
    return True


# ============================================================
# Face editing helpers
# ============================================================

def face_axis(face_key):
    return face_key[-1]


def face_sign(face_key):
    return -1.0 if face_key.startswith("min") else 1.0


def face_key_from_local_point(local_point, bounds):
    distances = {
        "min_x": abs(local_point.x - bounds["min_x"]),
        "max_x": abs(local_point.x - bounds["max_x"]),
        "min_y": abs(local_point.y - bounds["min_y"]),
        "max_y": abs(local_point.y - bounds["max_y"]),
        "min_z": abs(local_point.z - bounds["min_z"]),
        "max_z": abs(local_point.z - bounds["max_z"]),
    }
    return min(distances, key=distances.get)


def face_center_and_normal_world(obj, bounds, face_key):
    axis = face_axis(face_key)
    center = bounds_center(bounds)
    setattr(center, axis, bounds[face_key])

    local_normal = AXIS_VECTORS[axis] * face_sign(face_key)
    world_normal = (obj.matrix_world.to_3x3() @ local_normal).normalized()
    return obj.matrix_world @ center, world_normal


def coord_from_screen_drag(start_coord, mouse_delta, screen_axis, face_key):
    amount = mouse_delta.dot(screen_axis) * DRAG_SCALE
    return start_coord + amount * face_sign(face_key)


def snap_face_coord_world(obj, face_key, local_coord):
    axis = face_axis(face_key)
    world_axis = (obj.matrix_world.to_3x3() @ AXIS_VECTORS[axis]).normalized()
    world_origin = obj.matrix_world.translation

    world_coord = world_origin.dot(world_axis) + local_coord
    snapped_world_coord = snap_value(world_coord, EDIT_SNAP)
    return snapped_world_coord - world_origin.dot(world_axis)


def apply_face_coord(bounds, face_key, coord):
    result = bounds.copy()
    axis = face_axis(face_key)

    if axis not in {"x", "y"}:
        return normalize_bounds(result)

    extent = max(abs(float(coord)), EDIT_SNAP * 0.5)
    result[f"min_{axis}"] = -extent
    result[f"max_{axis}"] = extent
    result["min_z"] = 0.0
    result["max_z"] = DEFAULT_CUBE_SIZE

    return normalize_bounds(result)


def editable_cube_target(context):
    return active_cube(context)


# ============================================================
# Modal operator
# ============================================================

class ECB_OT_edit_cube(bpy.types.Operator):
    bl_idname = "editcube.edit"
    bl_label = "Edit"
    bl_description = "Ctrl+LMB create, Ctrl+RMB delete, RMB select, LMB face edit"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        if context.area.type != "VIEW_3D":
            self.report({"WARNING"}, "3D View only")
            return {"CANCELLED"}

        self.reset(active_cube(context))
        context.window_manager.modal_handler_add(self)
        self.set_header(context)
        return {"RUNNING_MODAL"}

    def reset(self, cube_obj=None):
        self.cube_obj = cube_obj
        self.dragging = False
        self.mode = None
        self.active_face = None
        self.start_bounds = cube_bounds(cube_obj).copy() if cube_obj and cube_bounds(cube_obj) else None
        self.start_mouse = Vector((0.0, 0.0))
        self.start_face_coord = 0.0
        self.screen_axis = None
        self.create_start_world = None
        self.created_obj = None

    def set_header(self, context):
        _add_mode_overlay(self, "Edit Cube: Create=1m snap / Edit=0.5m snap / "
                  "LMB edit / RMB select / Ctrl+LMB create / Ctrl+RMB delete / ESC exit")

    def begin_create(self, context, event):
        location = cube_location_from_mouse(context, event)
        if location is None:
            return False

        self.created_obj = create_cube(context, location)
        self.cube_obj = self.created_obj
        self.create_start_world = location.copy()
        self.dragging = True
        self.mode = "CREATE"
        _add_mode_overlay(self, "Edit Cube: Ctrl+drag to size new box / release confirm / ESC cancel")
        return True

    def begin_face_edit(self, context, event):
        target = editable_cube_target(context)
        if target is None:
            return False

        bounds = cube_bounds(target)
        if bounds is None:
            return False

        hit = raycast_object(context, event, target)
        if hit is None:
            return False

        local_hit = target.matrix_world.inverted() @ hit["location"]
        face_key = face_key_from_local_point(local_hit, bounds)
        if face_axis(face_key) == "z":
            return False

        face_origin, face_normal = face_center_and_normal_world(target, bounds, face_key)
        screen_axis = screen_axis_for_world_axis(context, face_origin, face_normal)
        if screen_axis is None:
            return False

        self.cube_obj = target
        self.start_bounds = bounds.copy()
        self.active_face = face_key
        self.start_mouse = Vector((event.mouse_region_x, event.mouse_region_y))
        self.start_face_coord = bounds[face_key]
        self.screen_axis = screen_axis
        self.dragging = True
        self.mode = "EDIT_FACE"
        _add_mode_overlay(self, f"Edit Cube: editing {target.name} / {face_key} / release continue / ESC exit")
        return True

    def update_create(self, context, event):
        if self.created_obj is None:
            return

        current = create_drag_location(context, event, self.create_start_world.z)
        if current is not None:
            update_cube_mesh(self.created_obj, bounds_from_create_drag(self.created_obj, self.create_start_world, current))

    def update_face_edit(self, event):
        if not self.cube_obj or self.start_bounds is None or self.screen_axis is None:
            return

        mouse = Vector((event.mouse_region_x, event.mouse_region_y))
        coord = coord_from_screen_drag(self.start_face_coord, mouse - self.start_mouse, self.screen_axis, self.active_face)
        coord = snap_face_coord_world(self.cube_obj, self.active_face, coord)
        update_cube_mesh(self.cube_obj, apply_face_coord(self.start_bounds, self.active_face, coord))

    def finish_drag(self, context):
        if self.cube_obj:
            select_only(context, self.cube_obj)
        self.reset(self.cube_obj)
        self.set_header(context)

    def cancel_or_finish(self, context):
        if self.dragging and self.mode == "CREATE" and self.created_obj and self.created_obj.name in bpy.data.objects:
            bpy.data.objects.remove(self.created_obj, do_unlink=True)
            _remove_mode_overlay(self)
            return {"CANCELLED"}

        _remove_mode_overlay(self)
        return {"CANCELLED" if self.dragging else "FINISHED"}

    def modal(self, context, event):
        if event.type == "ESC":
            return self.cancel_or_finish(context)

        if is_view_passthrough(event, allow_ctrl=True):
            return {"PASS_THROUGH"}

        if not self.dragging:
            if event.ctrl and event.type == "RIGHTMOUSE" and event.value == "PRESS":
                delete_cube_under_mouse(context, event)
                return {"RUNNING_MODAL"}

            if event.type == "RIGHTMOUSE" and event.value == "PRESS":
                select_cube_under_mouse(context, event)
                self.reset(active_cube(context))
                return {"RUNNING_MODAL"}

            if event.ctrl and event.type == "LEFTMOUSE" and event.value == "PRESS":
                self.begin_create(context, event)
                return {"RUNNING_MODAL"}

            if event.type == "LEFTMOUSE" and event.value == "PRESS":
                self.begin_face_edit(context, event)
                return {"RUNNING_MODAL"}

        if self.dragging and event.type == "MOUSEMOVE":
            if self.mode == "CREATE":
                self.update_create(context, event)
            elif self.mode == "EDIT_FACE":
                self.update_face_edit(event)
            return {"RUNNING_MODAL"}

        if self.dragging and event.type == "LEFTMOUSE" and event.value == "RELEASE":
            self.finish_drag(context)
            return {"RUNNING_MODAL"}

        return {"RUNNING_MODAL"}


# ============================================================
# UI
# ============================================================

class ECB_PT_panel(bpy.types.Panel):
    bl_label = "EditCube"
    bl_idname = "ECB_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "EditCube"

    def draw(self, context):
        layout = self.layout

        layout.operator("editcube.edit", text="Edit Cube")

        obj = context.object
        if is_editcube_object(obj):
            bounds = cube_bounds(obj)
            layout.separator()
            layout.label(text="Selected Cube")
            if bounds:
                sx = bounds["max_x"] - bounds["min_x"]
                sy = bounds["max_y"] - bounds["min_y"]
                sz = bounds["max_z"] - bounds["min_z"]
                layout.label(text=f"Size: {sx:.3f} x {sy:.3f} x {sz:.3f} m")


classes = (
    ECB_OT_edit_cube,
    ECB_PT_panel,
)


def register():
    for cls in classes:
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


if __name__ == "__main__":
    register()
