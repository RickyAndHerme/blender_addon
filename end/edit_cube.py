bl_info = {
    "name": "EditCube",
    "author": "ChatGPT",
    "version": (0, 5, 0),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > EditCube",
    "description": "Single-tool cube blockout: 1m placement snap, 0.5m face edit snap.",
    "category": "3D View",
}

import bpy
from mathutils import Matrix, Vector
from bpy_extras import view3d_utils


# ============================================================
# Constants
# ============================================================

ADDON_PREFIX = "ECB"
COLLECTION_NAME = "EditCube_Objects"
TAG_KEY = "editcube_tool"
KIND_KEY = "editcube_kind"
KIND_CUBE = "CUBE"

DEFAULT_CUBE_SIZE = 1.0
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
    if collection is None:
        collection = bpy.data.collections.new(COLLECTION_NAME)
        context.scene.collection.children.link(collection)
    return collection


def tag_cube(obj):
    obj[TAG_KEY] = True
    obj[KIND_KEY] = KIND_CUBE


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
    inv = obj.matrix_world.inverted()
    local_origin = inv @ origin
    local_direction = (inv.to_3x3() @ direction).normalized()

    hit, local_location, local_normal, face_index = obj.ray_cast(local_origin, local_direction)
    if not hit:
        return None

    return {
        "object": obj,
        "location": obj.matrix_world @ local_location,
        "normal": (obj.matrix_world.to_3x3() @ local_normal).normalized(),
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


def unit_bounds(size=DEFAULT_CUBE_SIZE):
    half = size * 0.5
    return {
        "min_x": -half,
        "max_x": half,
        "min_y": -half,
        "max_y": half,
        "min_z": -half,
        "max_z": half,
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
    if obj is None or obj.type != "MESH" or len(obj.data.vertices) < 8:
        return None

    coords = [v.co for v in obj.data.vertices]
    return normalize_bounds({
        "min_x": min(v.x for v in coords),
        "max_x": max(v.x for v in coords),
        "min_y": min(v.y for v in coords),
        "max_y": max(v.y for v in coords),
        "min_z": min(v.z for v in coords),
        "max_z": max(v.z for v in coords),
    })


def update_cube_mesh(obj, bounds):
    bounds = normalize_bounds(bounds)
    min_x, max_x = bounds["min_x"], bounds["max_x"]
    min_y, max_y = bounds["min_y"], bounds["max_y"]
    min_z, max_z = bounds["min_z"], bounds["max_z"]

    verts = [
        (min_x, min_y, min_z),
        (max_x, min_y, min_z),
        (max_x, max_y, min_z),
        (min_x, max_y, min_z),
        (min_x, min_y, max_z),
        (max_x, min_y, max_z),
        (max_x, max_y, max_z),
        (min_x, max_y, max_z),
    ]
    faces = [
        (0, 1, 2, 3),
        (4, 7, 6, 5),
        (0, 4, 5, 1),
        (1, 5, 6, 2),
        (2, 6, 7, 3),
        (3, 7, 4, 0),
    ]

    obj.data.clear_geometry()
    obj.data.from_pydata(verts, [], faces)
    obj.data.update()

    for key, value in bounds.items():
        obj[f"editcube_{key}"] = float(value)


def bounds_from_create_drag(obj, start_world, current_world):
    start_local = obj.matrix_world.inverted() @ start_world
    current_local = obj.matrix_world.inverted() @ current_world
    half = DEFAULT_CUBE_SIZE * 0.5

    return normalize_bounds({
        "min_x": snap_value(min(start_local.x, current_local.x, -half), EDIT_SNAP),
        "max_x": snap_value(max(start_local.x, current_local.x, half), EDIT_SNAP),
        "min_y": snap_value(min(start_local.y, current_local.y, -half), EDIT_SNAP),
        "max_y": snap_value(max(start_local.y, current_local.y, half), EDIT_SNAP),
        "min_z": -half,
        "max_z": half,
    })


# ============================================================
# Cube creation / deletion / origin
# ============================================================

def placement_location(context, event):
    hit = raycast_visible_mesh(context, event)
    if hit is not None:
        return snap_vector(hit["location"], CREATE_SNAP)

    projected = project_mouse_to_world_xy(context, event, z=0.0)
    return snap_vector(projected, CREATE_SNAP) if projected is not None else None


def create_drag_location(context, event, z):
    projected = project_mouse_to_world_xy(context, event, z=z)
    return snap_vector(projected, EDIT_SNAP) if projected is not None else None


def create_cube(context, location):
    mesh = bpy.data.meshes.new(f"{ADDON_PREFIX}_CubeMesh")
    obj = bpy.data.objects.new(f"{ADDON_PREFIX}_Cube", mesh)
    obj.matrix_world = Matrix.Translation(location)
    tag_cube(obj)
    update_cube_mesh(obj, unit_bounds())

    get_or_create_collection(context).objects.link(obj)
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


def recenter_cube_origin(obj):
    """Move origin to local bounds center while preserving world-space geometry."""
    if not is_editcube_object(obj):
        return False

    bounds = cube_bounds(obj)
    if bounds is None:
        return False

    local_center = bounds_center(bounds)
    if local_center.length < 1e-8:
        return True

    world_center = obj.matrix_world @ local_center
    for vert in obj.data.vertices:
        vert.co -= local_center
    obj.data.update()
    obj.matrix_world.translation = world_center
    return True


def recenter_all_cube_origins():
    for obj in bpy.data.objects:
        if is_editcube_object(obj):
            recenter_cube_origin(obj)


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
    min_key = f"min_{axis}"
    max_key = f"max_{axis}"

    if face_key.startswith("min"):
        result[min_key] = min(float(coord), bounds[max_key] - EDIT_SNAP)
    else:
        result[max_key] = max(float(coord), bounds[min_key] + EDIT_SNAP)

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
        context.area.header_text_set(
            "EditCube: Create=1m snap / Edit=0.5m snap / "
            "LMB edit / RMB select / Ctrl+LMB create / Ctrl+RMB delete / ESC exit"
        )

    def begin_create(self, context, event):
        location = placement_location(context, event)
        if location is None:
            return False

        self.created_obj = create_cube(context, location)
        self.cube_obj = self.created_obj
        self.create_start_world = location.copy()
        self.dragging = True
        self.mode = "CREATE"
        context.area.header_text_set("EditCube: Ctrl+drag to size new box / release confirm / ESC cancel")
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
        context.area.header_text_set(f"EditCube: editing {target.name} / {face_key} / release continue / ESC exit")
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
            recenter_cube_origin(self.cube_obj)
            select_only(context, self.cube_obj)
        self.reset(self.cube_obj)
        self.set_header(context)

    def cancel_or_finish(self, context):
        if self.dragging and self.mode == "CREATE" and self.created_obj and self.created_obj.name in bpy.data.objects:
            bpy.data.objects.remove(self.created_obj, do_unlink=True)
            context.area.header_text_set(None)
            return {"CANCELLED"}

        recenter_all_cube_origins()
        context.area.header_text_set(None)
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
        layout.operator("editcube.edit", text="Edit")

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
