bl_info = {
    "name": "GridArch",
    "author": "Zeroni",
    "version": (0, 6, 11),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > GridArch",
    "description": "An architectural blockout toolkit featuring grid-based tile editing, wall generation, and the creation of openings via Boolean operations, complete with collision primitives for Godot.",
    "category": "3D View",
}

import bpy
import bmesh
from bpy.app.handlers import persistent
from collections import defaultdict
from math import floor
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    PointerProperty,
    StringProperty,
)
from mathutils import Matrix, Vector
from bpy_extras import view3d_utils

ADDON_PREFIX = "GA"
ECB_COLLECTION_NAME = "EditCube_Objects"
ECB_TAG_KEY = "editcube_tool"
ECB_KIND_KEY = "editcube_kind"
ECB_KIND_CUBE = "CUBE"
ECB_MIRROR_MOD_NAME = "ECB_Mirror_XY"
ECB_LEGACY_MIRROR_MOD_NAMES = ("ECB_Mirror_X", "ECB_Mirror_Y")
ECB_SOLIDIFY_MOD_NAME = "ECB_Solidify_1m"

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
    # Do not create a dedicated collection automatically; return existing if present,
    # otherwise fall back to the scene master collection.
    collection = bpy.data.collections.get(ECB_COLLECTION_NAME)
    if collection is not None:
        return collection
    return context.scene.collection


def tag_cube(obj):
    obj[ECB_TAG_KEY] = True
    obj[ECB_KIND_KEY] = ECB_KIND_CUBE
    if getattr(obj, "data", None) is not None:
        obj.data[ECB_TAG_KEY] = True
        obj.data[ECB_KIND_KEY] = ECB_KIND_CUBE


def is_editcube_object(obj):
    return obj is not None and obj.type == "MESH" and obj.get(ECB_TAG_KEY) is True


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


# ============================================================
# Screen axis helpers
# ============================================================

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


def snap_xy_vector(v, step):
    return Vector((snap_value(v.x, step), snap_value(v.y, step), v.z))


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
    mirror = obj.modifiers.get(ECB_MIRROR_MOD_NAME)
    if mirror is None:
        for legacy_name in ECB_LEGACY_MIRROR_MOD_NAMES:
            legacy = obj.modifiers.get(legacy_name)
            if legacy is not None and legacy.type == "MIRROR":
                mirror = legacy
                mirror.name = ECB_MIRROR_MOD_NAME
                break
    if mirror is None or mirror.type != "MIRROR":
        mirror = obj.modifiers.new(ECB_MIRROR_MOD_NAME, "MIRROR")
    mirror.use_axis = (True, True, False)
    mirror.use_clip = True
    mirror.use_mirror_merge = True
    mirror.merge_threshold = 0.001

    for legacy_name in ECB_LEGACY_MIRROR_MOD_NAMES:
        legacy = obj.modifiers.get(legacy_name)
        if legacy is not None:
            obj.modifiers.remove(legacy)

    solidify = obj.modifiers.get(ECB_SOLIDIFY_MOD_NAME)
    if solidify is None or solidify.type != "SOLIDIFY":
        solidify = obj.modifiers.new(ECB_SOLIDIFY_MOD_NAME, "SOLIDIFY")
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

def cube_location_from_mouse(context, event, land_on_bounds: bool = False):
    hit = raycast_visible_mesh(context, event)
    if hit is not None:
        if land_on_bounds:
            # Find the highest bounding-box top Z among all mesh objects whose
            # world-space bbox covers the hit XY. This ensures clicking inside
            # a room lands on surrounding walls' bbox top instead of the floor
            # face if the floor is a separate lower mesh.
            hit_x = hit["location"].x
            hit_y = hit["location"].y
            best_top = None
            tol = 1e-6
            for obj in bpy.data.objects:
                if not obj or obj.type != "MESH":
                    continue
                try:
                    corners = _world_bbox_corners(obj)
                except Exception:
                    continue
                minx = min(c.x for c in corners)
                maxx = max(c.x for c in corners)
                miny = min(c.y for c in corners)
                maxy = max(c.y for c in corners)
                if (minx - tol) <= hit_x <= (maxx + tol) and (miny - tol) <= hit_y <= (maxy + tol):
                    topz = max(c.z for c in corners)
                    if best_top is None or topz > best_top:
                        best_top = topz
            if best_top is not None:
                location = Vector((hit["location"].x, hit["location"].y, best_top))
                return snap_xy_vector(location, CREATE_SNAP)
        location = snap_xy_vector(hit["location"], CREATE_SNAP)
        return location

    projected = project_mouse_to_world_xy(context, event, z=0.0)
    return snap_vector(projected, CREATE_SNAP) if projected is not None else Vector((0.0, 0.0, 0.0))


def create_drag_location(context, event, z):
    projected = project_mouse_to_world_xy(context, event, z=z)
    return snap_vector(projected, EDIT_SNAP) if projected is not None else None


def create_cube(context, location):
    mesh = bpy.data.meshes.new(f"{ADDON_PREFIX}_ECubeMesh")
    obj = bpy.data.objects.new(f"{ADDON_PREFIX}_ECube", mesh)
    obj.matrix_world = Matrix.Translation(Vector((location.x, location.y, location.z)))
    tag_cube(obj)
    update_cube_mesh(obj, default_cube_bounds())

    # Link into the scene master collection (do not create a dedicated collection)
    scene_collection(context).objects.link(obj)
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

    land_on_bounds: BoolProperty(
        name="Land on BBox Top",
        default=True,
        description="Land new EditCube at the hit object's bounding-box top instead of the hit surface.",
    )

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
        location = cube_location_from_mouse(context, event, land_on_bounds=self.land_on_bounds)
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


# ----------------------------
# Utilities
# ----------------------------

def scene_collection(context=None) -> bpy.types.Collection:
    """Return the active scene master collection.

    GridArch does not create, require, or manage a dedicated collection.
    Object grouping is handled by names and custom tags only.
    """
    ctx = context or bpy.context
    return ctx.scene.collection


def _add_mode_overlay(owner, label: str):
    """Display mode label in the View3D header."""
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.header_text_set(label)
                    owner._mode_area = area
                    return
    except Exception:
        pass


def _remove_mode_overlay(owner):
    """Clear mode label from the View3D header."""
    try:
        area = getattr(owner, '_mode_area', None)
        if area:
            area.header_text_set(None)
    except Exception:
        pass
    owner._mode_area = None




def raycast_to_ground(context, event, ground_z: float = 0.0):
    """Raycast mouse to a plane Z=ground_z in world coordinates.

    Returns (hit: bool, location: Vector)
    """
    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None:
        return False, None

    coord = (event.mouse_region_x, event.mouse_region_y)
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)

    # Intersect ray with plane z=ground_z
    # origin + t*direction, solve for z
    if abs(direction.z) < 1e-8:
        return False, None

    t = (ground_z - origin.z) / direction.z
    if t < 0:
        return False, None

    hit = origin + direction * t
    return True, hit



def tag_object(obj: bpy.types.Object, tag_key: str, tag_val: str):
    obj[tag_key] = tag_val



def is_tagged(obj: bpy.types.Object, tag_key: str, tag_val: str) -> bool:
    return bool(obj and obj.get(tag_key) == tag_val)


def gridarch_settings(context):
    """Return GridArch settings.

    `gridmap3d` remains as a compatibility alias, but new code should use this helper.
    """
    return getattr(context.scene, "gridarch", None) or getattr(context.scene, "gridmap3d", None)


def _world_bbox_corners(obj: bpy.types.Object):
    """Return 8 world-space corners for object's bound_box."""
    bb_local = [Vector(corner) for corner in obj.bound_box]
    mw = obj.matrix_world
    return [mw @ v for v in bb_local]


def _world_bbox_center(obj: bpy.types.Object) -> Vector:
    corners = _world_bbox_corners(obj)
    c = Vector((0.0, 0.0, 0.0))
    for v in corners:
        c += v
    return c / max(1, len(corners))


def _oriented_bbox_size(obj: bpy.types.Object) -> Vector:
    """Return the object's bounding-box size in its own local axes, including object scale.

    This is different from world AABB size. It preserves the intended cutter size
    when the source object is rotated.
    """
    bb = [Vector(corner) for corner in obj.bound_box]
    min_l = Vector((min(v.x for v in bb), min(v.y for v in bb), min(v.z for v in bb)))
    max_l = Vector((max(v.x for v in bb), max(v.y for v in bb), max(v.z for v in bb)))
    local_size = max_l - min_l
    scale = obj.matrix_world.to_scale()
    return Vector((
        abs(local_size.x * scale.x),
        abs(local_size.y * scale.y),
        abs(local_size.z * scale.z),
    ))


def active_plane_related_size_m(context):
    """Return size for the active GridArch_Plane and its related wall objects only.

    Scope:
    - active plane-kind mesh, or GridArch_Plane fallback
    - matching OuterWall derived from the plane name
    - GridArch_DrawWall objects whose stored edges overlap the plane footprint

    Collision objects and unrelated GridArch objects are intentionally ignored.
    """
    plane = get_active_or_default_plane(context)
    if not plane or plane.type != 'MESH':
        return None

    targets = [plane]

    # Matching outer wall, derived from selected plane name.
    outer_name = plane.name.replace("Plane", "AutoWall") if "Plane" in plane.name else f"{plane.name}_AutoWall"
    outer = bpy.data.objects.get(outer_name)
    if outer and outer.type == 'MESH' and is_tagged(outer, TAG_KEY, TAG_VAL):
        targets.append(outer)

    # Determine plane footprint cells so unrelated GridArch_DrawWall objects do not pollute size.
    s = gridarch_settings(context)
    plane_cells = set()
    if plane.data.polygons:
        bm = bmesh.new()
        bm.from_mesh(plane.data)
        for f in bm.faces:
            c = _plane_face_center_local(f)
            plane_cells.add((int(floor(c.x / float(s.grid_size))), int(floor(c.y / float(s.grid_size)))))
        bm.free()

    if plane_cells:
        for obj in bpy.data.objects:
            if not (obj and obj.type == 'MESH' and is_tagged(obj, TAG_KEY, TAG_VAL) and obj.get(WALL_KIND_KEY) == WALL_KIND_VAL):
                continue
            edges = _parse_edges_string(obj.get("ga_wall_edges", ""))
            if not edges:
                continue
            for a, b in edges:
                if (a[0], a[1]) in plane_cells or (b[0], b[1]) in plane_cells:
                    targets.append(obj)
                    break

    corners = []
    for obj in targets:
        corners.extend(_world_bbox_corners(obj))

    if not corners:
        return None

    min_v = Vector((min(v.x for v in corners), min(v.y for v in corners), min(v.z for v in corners)))
    max_v = Vector((max(v.x for v in corners), max(v.y for v in corners), max(v.z for v in corners)))
    size = max_v - min_v
    return float(size.x), float(size.y), float(size.z)


# ----------------------------
# Shared mesh helpers (single-object plane painting)
# ----------------------------

GRID_PLANE_NAME = "GA_Plane"
PLANE_KIND_KEY = "ga_kind"
PLANE_KIND_VAL = "PLANE"


def get_active_or_default_plane(context) -> bpy.types.Object | None:
    """Return active GA plane-kind mesh, otherwise the default GA_Plane object."""
    obj = context.view_layer.objects.active
    if obj and obj.type == 'MESH' and is_tagged(obj, TAG_KEY, TAG_VAL) and obj.get(PLANE_KIND_KEY) == PLANE_KIND_VAL:
        return obj
    obj = bpy.data.objects.get(GRID_PLANE_NAME)
    return obj if obj and obj.type == 'MESH' else None


def ensure_grid_plane_object(context) -> bpy.types.Object:
    """Ensure a mesh object used for plane painting.

    No collection management: use the active plane-kind object if possible,
    otherwise use/create GA_Plane in the scene master collection.
    """
    obj = get_active_or_default_plane(context)
    if obj:
        obj[PLANE_KIND_KEY] = PLANE_KIND_VAL
        return obj

    me = bpy.data.meshes.new(GRID_PLANE_NAME + "_Mesh")
    obj = bpy.data.objects.new(GRID_PLANE_NAME, me)
    scene_collection(context).objects.link(obj)
    tag_object(obj, TAG_KEY, TAG_VAL)
    obj[PLANE_KIND_KEY] = PLANE_KIND_VAL
    obj.location = Vector((0.0, 0.0, 0.0))
    return obj


def _plane_face_center_local(face: bmesh.types.BMFace) -> Vector:
    c = Vector((0.0, 0.0, 0.0))
    for v in face.verts:
        c += v.co
    return c / max(1, len(face.verts))


def _face_bbox_xy(face: bmesh.types.BMFace):
    """Return (minx, maxx, miny, maxy) for a face in local XY space."""
    xs = [v.co.x for v in face.verts]
    ys = [v.co.y for v in face.verts]
    return min(xs), max(xs), min(ys), max(ys)


def _add_plane_rect_face(bm: bmesh.types.BMesh, minx: float, maxx: float, miny: float, maxy: float, z: float):
    """Add one rectangular horizontal face to a BMesh."""
    verts = (
        bm.verts.new((minx, miny, z)),
        bm.verts.new((maxx, miny, z)),
        bm.verts.new((maxx, maxy, z)),
        bm.verts.new((minx, maxy, z)),
    )
    try:
        bm.faces.new(verts)
    except ValueError:
        pass


def add_grid_plane_tile(context, cell: tuple[int, int, int], grid_size: float):
    """Add one quad tile to the shared plane mesh at a given integer cell."""
    obj = ensure_grid_plane_object(context)
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)

    x, y, z = cell
    minx = x * grid_size
    maxx = (x + 1) * grid_size
    miny = y * grid_size
    maxy = (y + 1) * grid_size
    cx = (minx + maxx) * 0.5
    cy = (miny + maxy) * 0.5
    cz = z * Z_UNIT

    # Avoid duplicates: check face centers
    tol = 1e-4
    for f in bm.faces:
        c = _plane_face_center_local(f)
        if abs(c.x - cx) < tol and abs(c.y - cy) < tol and abs(c.z - cz) < tol:
            bm.free()
            return

    v0 = bm.verts.new((minx, miny, cz))
    v1 = bm.verts.new((maxx, miny, cz))
    v2 = bm.verts.new((maxx, maxy, cz))
    v3 = bm.verts.new((minx, maxy, cz))
    try:
        bm.faces.new((v0, v1, v2, v3))
    except ValueError:
        pass

    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    me.update()


def remove_grid_plane_cell_and_connected_line(context, cell: tuple[int, int, int], grid_size: float) -> bool:
    """Delete whichever plane face covers the clicked cell.

    This supports both:
    - regular square tiles
    - optimized rectangular faces created by OptimizeLine

    No linked deletion. It just removes the face under the clicked cell footprint.
    """
    obj = get_active_or_default_plane(context)
    if not obj:
        return False

    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)

    x, y, z = cell
    cx = (x + 0.5) * grid_size
    cy = (y + 0.5) * grid_size
    cz = z * Z_UNIT
    tol = 1e-4

    to_delete = []
    for f in bm.faces:
        c = _plane_face_center_local(f)
        if abs(c.z - cz) >= tol:
            continue

        minx, maxx, miny, maxy = _face_bbox_xy(f)
        sx = maxx - minx
        sy = maxy - miny

        # square tile: delete only when center matches the clicked cell center
        is_square_tile = abs(sx - grid_size) < tol and abs(sy - grid_size) < tol
        if is_square_tile:
            if abs(c.x - cx) < tol and abs(c.y - cy) < tol:
                to_delete.append(f)
            continue

        # rectangle: delete when clicked cell center lies inside the rectangle footprint
        if (minx - tol) <= cx <= (maxx + tol) and (miny - tol) <= cy <= (maxy + tol):
            to_delete.append(f)

    if not to_delete:
        bm.free()
        return False

    bmesh.ops.delete(bm, geom=to_delete, context='FACES')

    loose_edges = [e for e in bm.edges if len(e.link_faces) == 0]
    if loose_edges:
        bmesh.ops.delete(bm, geom=loose_edges, context='EDGES')
    loose_verts = [v for v in bm.verts if len(v.link_edges) == 0]
    if loose_verts:
        bmesh.ops.delete(bm, geom=loose_verts, context='VERTS')

    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    me.update()
    return True


def _collect_plane_cells_by_bbox(obj: bpy.types.Object, grid_size: float) -> dict[float, set[tuple[int, int]]]:
    """Collect occupied grid cells from GridArch_Plane faces using exact rasterization.

    This gathers cells by sampling the actual face interior rather than just the face bbox.
    It prevents Optimize from accidentally filling cells that are only within a face bbox.
    """
    cells_by_z = defaultdict(set)
    if not obj or obj.type != 'MESH' or not obj.data.polygons:
        return cells_by_z

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    for f in bm.faces:
        z = round(float(_plane_face_center_local(f).z), 6)
        cells = _rasterize_face_cells(f, grid_size)
        if cells:
            cells_by_z[z].update(cells)
    bm.free()
    return cells_by_z


def rebuild_plane_as_max_rectangles(obj: bpy.types.Object, grid_size: float) -> int:
    """Rebuild GridArch_Plane faces as maximal rectangles per occupied cell set.

    This preserves exact occupied cells and avoids filling empty holes while still
    merging tiled floor blockouts into larger rectangular faces.
    """
    cells_by_z = _collect_plane_cells_by_bbox(obj, grid_size)
    if not cells_by_z:
        return 0

    new_bm = bmesh.new()
    face_count = 0

    for z_key, cells in sorted(cells_by_z.items(), key=lambda item: item[0]):
        cells = set(cells)
        while cells:
            # Start from the lowest row and leftmost cell.
            y0 = min(y for _, y in cells)
            x0 = min(x for x, y in cells if y == y0)

            x1 = x0
            while (x1 + 1, y0) in cells:
                x1 += 1

            y1 = y0
            while True:
                next_y = y1 + 1
                if all((x, next_y) in cells for x in range(x0, x1 + 1)):
                    y1 = next_y
                else:
                    break

            for yy in range(y0, y1 + 1):
                for xx in range(x0, x1 + 1):
                    cells.discard((xx, yy))

            _add_plane_rect_face(
                new_bm,
                x0 * grid_size,
                (x1 + 1) * grid_size,
                y0 * grid_size,
                (y1 + 1) * grid_size,
                float(z_key),
            )
            face_count += 1

    bmesh.ops.remove_doubles(new_bm, verts=new_bm.verts, dist=1e-6)
    new_bm.normal_update()
    new_bm.to_mesh(obj.data)
    new_bm.free()
    obj.data.update()
    return face_count


def optimize_wall_mesh(obj: bpy.types.Object) -> int:
    """Clean GridArch_DrawWall mesh after box-segment generation.

    This does not try to boolean-union wall prisms. It safely removes duplicate
    vertices and dissolves only flat coplanar edges, which improves L/T/+ wall
    meshes without changing the wall footprint.
    """
    if not obj or obj.type != 'MESH' or not obj.data.polygons:
        return 0

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    before_edges = len(bm.edges)

    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    try:
        bmesh.ops.dissolve_limit(
            bm,
            angle_limit=0.0001,
            verts=bm.verts,
            edges=bm.edges,
            use_dissolve_boundaries=False,
        )
    except Exception:
        pass

    bm.normal_update()
    bm.to_mesh(obj.data)
    after_edges = len(bm.edges)
    bm.free()
    obj.data.update()
    return max(0, before_edges - after_edges)


def make_mesh_data_undo_safe(obj: bpy.types.Object):
    """Give an object its own mesh datablock before destructive BMesh edits.

    This makes Blender's operator Undo more reliable for Optimize-like operations.
    Without this, direct BMesh edits on shared/current mesh data can sometimes undo
    to an empty or partially updated mesh instead of the pre-optimized state.
    """
    if obj and obj.type == 'MESH' and obj.data:
        obj.data = obj.data.copy()


def optimize_keep_right_angles(obj: bpy.types.Object, angle_tol: float = 1e-3) -> int:
    """Dissolve redundant boundary vertices while preserving right-angle corners.

    Important: this function only dissolves boundary vertices. Dissolving all non-corner
    vertices can destroy filled tile faces and makes undo appear broken because the mesh
    itself has already collapsed. Keep internal verts/edges untouched here.
    """
    if not obj or obj.type != 'MESH' or not obj.data.polygons:
        return 0

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    boundary_edges = [e for e in bm.edges if len(e.link_faces) == 1]
    if not boundary_edges:
        bm.free()
        return 0

    v2bedges = defaultdict(list)
    for e in boundary_edges:
        v2bedges[e.verts[0]].append(e)
        v2bedges[e.verts[1]].append(e)

    corner_verts = set()
    tol = float(angle_tol)

    for v, es in v2bedges.items():
        if len(es) != 2:
            corner_verts.add(v)
            continue

        e0, e1 = es
        d0 = e0.other_vert(v).co - v.co
        d1 = e1.other_vert(v).co - v.co
        d0.z = 0.0
        d1.z = 0.0

        len0 = d0.length
        len1 = d1.length
        if len0 < 1e-8 or len1 < 1e-8:
            corner_verts.add(v)
            continue

        cos_theta = d0.dot(d1) / (len0 * len1)
        if abs(cos_theta) <= tol:
            corner_verts.add(v)

    # Only boundary vertices are eligible for dissolve. Internal verts are never touched.
    to_dissolve = [v for v in v2bedges.keys() if v not in corner_verts]

    if not to_dissolve:
        bm.free()
        return 0

    dissolved = len(to_dissolve)
    bmesh.ops.dissolve_verts(bm, verts=to_dissolve)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bm.normal_update()
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    return dissolved


# ----------------------------
# GridArch Wall (from PlaneMesh) + BB Cube (minimal)
# ----------------------------

OUTER_NAME = "GA_AutoWall"


class GA_OT_create_outer_wall(Operator):
    bl_idname = "ga.create_outer_wall"
    bl_label = "Create Auto Wall (from selected Plane)"
    bl_options = {'REGISTER', 'UNDO'}

    rim_width: FloatProperty(name="Rim Width (m)", default=0.1, min=0.0)
    wall_height: FloatProperty(name="Wall Height (m)", default=1.0, min=0.0)

    def execute(self, context):
        s = gridarch_settings(context)
        # No collection management. Objects stay in the current scene collection.

        # Target MUST be the active Plane-kind object (supports GridArch_Plane.001 etc.)
        src = context.view_layer.objects.active
        if not (src and src.type == 'MESH' and is_tagged(src, TAG_KEY, TAG_VAL) and src.get(PLANE_KIND_KEY) == PLANE_KIND_VAL):
            self.report({'WARNING'}, "Select an active GA_Plane object first.")
            return {'CANCELLED'}

        if not src.data.polygons:
            self.report({'WARNING'}, "The selected plane has no faces. Paint plane tiles first.")
            return {'CANCELLED'}

        # Wall name is derived from selected plane (so duplicates get their own wall)
        wall_name = src.name.replace("Plane", "AutoWall") if "Plane" in src.name else f"{src.name}_AutoWall"

        me_outer = build_auto_wall_mesh_from_plane(src, float(self.rim_width))
        if me_outer is None:
            self.report({'WARNING'}, "Failed to create the outer rim strip from inset.")
            return {'CANCELLED'}
        me_outer.name = wall_name

        outer = bpy.data.objects.get(wall_name)
        if outer and outer.type == 'MESH':
            outer.data = me_outer
            # Keep existing collection membership untouched.

        else:
            outer = bpy.data.objects.new(wall_name, me_outer)
            scene_collection(context).objects.link(outer)

        tag_object(outer, TAG_KEY, TAG_VAL)
        outer.name = wall_name

        # Persist dimensions so Create Wall can match them later.
        # thickness: rim width, height: wall height
        outer["gm_wall_thickness"] = float(self.rim_width)
        outer["gm_wall_height"] = float(self.wall_height)
        outer["ga_auto_wall_source"] = src.name
        outer["ga_auto_wall"] = True

        # Parent OuterWall to the source plane so it follows the tile object.
        outer.parent = src
        outer.matrix_parent_inverse = src.matrix_world.inverted()
        outer.matrix_world = src.matrix_world.copy()
        add_or_get_wall_gn_modifier(outer, height=float(self.wall_height), thickness=float(self.rim_width))

        # Do not sync existing DrawWall objects here; AutoWall settings should only affect the created AutoWall.
        s.wall_thickness_default = float(self.rim_width)
        s.wall_height_default = float(self.wall_height)

        outer.select_set(True)
        context.view_layer.objects.active = outer
        return {'FINISHED'}


class BB_OT_create_cube(Operator):
    bl_idname = "object.bb_create_cube"
    bl_label = "Create Collision"
    bl_options = {'REGISTER', 'UNDO'}

    primitive: EnumProperty(
        name="Primitive",
        items=[
            ("CUBE", "Cube", "Create box collision"),
            ("SPHERE", "Sphere", "Create UV sphere collision"),
            ("ICO_SPHERE", "Ico Sphere", "Create ico sphere collision"),
            ("CYLINDER", "Cylinder", "Create cylinder collision"),
            ("CONE", "Cone", "Create cone collision"),
        ],
        default="CUBE",
    )

    bbox_size: BoolProperty(
        name="BBox Size",
        default=True,
        description="Fit the collision primitive to the selected object bounding box.",
    )

    size: FloatProperty(
        name="Scale",
        default=1.0,
        min=0.001,
        description="Scale multiplier for the generated collision primitive.",
    )

    per_object: BoolProperty(
        name="Per Object",
        default=False,
        description="Create one -colonly collision object per selected mesh instead of one combined collision.",
    )

    def execute(self, context):
        sel = [o for o in context.selected_objects if o and o.type == 'MESH']
        if not sel:
            self.report({'WARNING'}, "Select mesh object(s)")
            return {'CANCELLED'}

        depsgraph = context.evaluated_depsgraph_get()

        def obj_world_bbox(o: bpy.types.Object):
            eo = o.evaluated_get(depsgraph)
            corners = [eo.matrix_world @ Vector(c) for c in eo.bound_box]
            min_v = Vector((min(v.x for v in corners), min(v.y for v in corners), min(v.z for v in corners)))
            max_v = Vector((max(v.x for v in corners), max(v.y for v in corners), max(v.z for v in corners)))
            return min_v, max_v

        def remove_existing_collision(name: str):
            existing = bpy.data.objects.get(name)
            if existing:
                bpy.data.objects.remove(existing, do_unlink=True)

        def make_collision(name: str, parent: bpy.types.Object, min_v: Vector, max_v: Vector):
            remove_existing_collision(name)

            size = max_v - min_v
            center = (min_v + max_v) * 0.5
            primitive = self.primitive

            if self.bbox_size:
                base_size = (
                    max(float(size.x), 0.001),
                    max(float(size.y), 0.001),
                    max(float(size.z), 0.001),
                )
            else:
                base_size = (1.0, 1.0, 1.0)

            mesh = bpy.data.meshes.new(f"{name}_Mesh")
            col = bpy.data.objects.new(name, mesh)
            scene_collection(context).objects.link(col)
            col.location = center
            add_cutter_cube_geometry_nodes(
                col,
                base_size[0],
                base_size[1],
                base_size[2],
                primitive=primitive,
                scale=max(float(self.size), 0.001),
            )

            col.name = name
            col.display_type = 'WIRE'
            col.show_in_front = True
            col.hide_render = True
            col.parent = parent
            col.matrix_parent_inverse = parent.matrix_world.inverted()
            tag_object(col, TAG_KEY, TAG_VAL)
            col["ga_collision"] = True
            col["ga_collision_primitive"] = primitive
            return col

        made = 0
        if self.per_object:
            for obj in sel:
                min_v, max_v = obj_world_bbox(obj)
                make_collision(f"{obj.name}-colonly", obj, min_v, max_v)
                made += 1
        else:
            mins = []
            maxs = []
            for obj in sel:
                min_v, max_v = obj_world_bbox(obj)
                mins.append(min_v)
                maxs.append(max_v)
            min_all = Vector((min(v.x for v in mins), min(v.y for v in mins), min(v.z for v in mins)))
            max_all = Vector((max(v.x for v in maxs), max(v.y for v in maxs), max(v.z for v in maxs)))
            parent = context.view_layer.objects.active if context.view_layer.objects.active in sel else sel[0]
            make_collision("GA_Col-colonly", parent, min_all, max_all)
            made = 1

        self.report({'INFO'}, f"Created {made} collision object(s)")
        return {'FINISHED'}


def is_wall_boolean_target(obj: bpy.types.Object) -> bool:
    if not obj or obj.type != 'MESH':
        return False
    if obj.get(WALL_KIND_KEY) == WALL_KIND_VAL:
        return True
    return obj.name.startswith(OUTER_NAME) or "OuterWall" in obj.name or "AutoWall" in obj.name or bool(obj.get("ga_auto_wall"))


def find_boolean_wall_targets(context, exclude: set[bpy.types.Object] | None = None) -> list[bpy.types.Object]:
    """Find explicitly selected Wall/AutoWall objects that should receive Boolean difference modifiers."""
    exclude = exclude or set()
    selected = [o for o in context.selected_objects if o and o not in exclude]

    targets = []
    for obj in selected:
        if is_wall_boolean_target(obj) and obj not in targets:
            targets.append(obj)
    return targets


def _new_group_socket(node_group, name: str, in_out: str, socket_type: str):
    """Create a Geometry Nodes interface socket with Blender 4/5 compatible fallback."""
    socket = None
    try:
        socket = node_group.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)
    except Exception:
        # Older fallback; harmless if unavailable.
        if in_out == 'INPUT':
            socket = node_group.inputs.new(socket_type, name)
        else:
            socket = node_group.outputs.new(socket_type, name)

    identifier = getattr(socket, "identifier", None) or getattr(socket, "name", None)
    if identifier:
        node_group[f"ga_socket_{name}"] = identifier
    return socket


def _socket_identifier(node_group, socket_name: str):
    if node_group and f"ga_socket_{socket_name}" in node_group:
        return node_group[f"ga_socket_{socket_name}"]
    try:
        for item in node_group.interface.items_tree:
            if getattr(item, "item_type", None) == 'SOCKET' and getattr(item, "name", "") == socket_name:
                return getattr(item, "identifier", None)
    except Exception:
        pass
    try:
        socket = node_group.inputs.get(socket_name)
        return getattr(socket, "identifier", None) or socket_name
    except Exception:
        return None


def _set_modifier_socket(mod: bpy.types.Modifier, socket_name: str, value):
    ng = getattr(mod, "node_group", None)
    identifier = _socket_identifier(ng, socket_name) if ng else None
    if identifier:
        try:
            mod[identifier] = value
        except Exception:
            pass


def _get_modifier_socket(mod: bpy.types.Modifier, socket_name: str, default=None):
    ng = getattr(mod, "node_group", None)
    identifier = _socket_identifier(ng, socket_name) if ng else None
    if identifier and identifier in mod:
        return mod[identifier]
    if ng:
        try:
            for item in ng.interface.items_tree:
                if getattr(item, "item_type", None) == 'SOCKET' and getattr(item, "name", "") == socket_name:
                    ident = getattr(item, "identifier", None)
                    if ident and ident in mod:
                        return mod[ident]
        except Exception:
            pass
        try:
            # Fallback to the node group's input default value so UI drags update continuously
            sock = ng.inputs.get(socket_name)
            if sock is not None:
                val = getattr(sock, "default_value", None)
                if val is not None:
                    return val
        except Exception:
            pass
    return default


GA_WALL_GN_MOD = "GA_Wall_GN"
GA_WALL_GN_GROUP = "GA_Wall_Height_GN"
GA_WALL_GN_VERSION = 12


def _node_input(node, *names, index=None):
    for name in names:
        socket = node.inputs.get(name)
        if socket is not None:
            return socket
    return node.inputs[index] if index is not None and len(node.inputs) > index else None


def _node_output(node, *names, index=None):
    for name in names:
        socket = node.outputs.get(name)
        if socket is not None:
            return socket
    return node.outputs[index] if index is not None and len(node.outputs) > index else None


def get_or_create_wall_height_node_group() -> bpy.types.NodeTree:
    """GN group: footprint mesh is extruded by float Height, width is stored for sync."""
    ng = bpy.data.node_groups.get(GA_WALL_GN_GROUP)
    if ng and ng.get("ga_wall_gn_version") == GA_WALL_GN_VERSION:
        return ng

    if ng is None:
        ng = bpy.data.node_groups.new(GA_WALL_GN_GROUP, 'GeometryNodeTree')
    if hasattr(ng, "is_modifier"):
        ng.is_modifier = True

    try:
        ng.interface.clear()
    except Exception:
        pass
    _new_group_socket(ng, "Geometry", 'INPUT', 'NodeSocketGeometry')
    height_socket = _new_group_socket(ng, "Height", 'INPUT', 'NodeSocketFloat')
    thickness_socket = _new_group_socket(ng, "Thickness", 'INPUT', 'NodeSocketFloat')
    _new_group_socket(ng, "Geometry", 'OUTPUT', 'NodeSocketGeometry')
    try:
        height_socket.default_value = 1.0
        height_socket.min_value = 0.0
        height_socket.soft_max_value = 10.0
    except Exception:
        pass
    for socket, default, min_value in ((thickness_socket, 0.1, 0.001),):
        try:
            socket.default_value = default
            socket.min_value = min_value
            socket.soft_max_value = 10.0
        except Exception:
            pass

    nodes = ng.nodes
    links = ng.links
    nodes.clear()

    group_in = nodes.new('NodeGroupInput')
    extrude = nodes.new('GeometryNodeExtrudeMesh')
    store_thickness = nodes.new('GeometryNodeStoreNamedAttribute')
    group_out = nodes.new('NodeGroupOutput')
    try:
        extrude.inputs["Individual"].default_value = True
        _node_input(extrude, "Offset", index=2).default_value = (0.0, 0.0, 1.0)
        store_thickness.data_type = 'FLOAT'
        store_thickness.domain = 'FACE'
        store_thickness.inputs["Name"].default_value = "ga_wall_thickness"
    except Exception:
        pass

    group_in.location = (-720, 0)
    extrude.location = (-420, 80)
    store_thickness.location = (-120, 80)
    group_out.location = (180, 80)

    links.new(_node_output(group_in, "Geometry", index=0), _node_input(extrude, "Mesh", index=0))
    links.new(_node_output(group_in, "Height", index=1), _node_input(extrude, "Offset Scale", index=3))
    links.new(_node_output(extrude, "Mesh", index=0), _node_input(store_thickness, "Geometry", index=0))
    links.new(_node_output(group_in, "Thickness", index=2), _node_input(store_thickness, "Value", index=3))
    links.new(_node_output(store_thickness, "Geometry", index=0), _node_input(group_out, "Geometry", index=0))

    ng["ga_wall_gn_version"] = GA_WALL_GN_VERSION
    return ng


def add_or_get_wall_gn_modifier(obj: bpy.types.Object, height: float, thickness: float, reset_inputs: bool = True):
    mod = obj.modifiers.get(GA_WALL_GN_MOD)
    ng = get_or_create_wall_height_node_group()
    if mod is None or mod.type != 'NODES':
        mod = obj.modifiers.new(name=GA_WALL_GN_MOD, type='NODES')
        mod.node_group = ng
    else:
        mod.name = GA_WALL_GN_MOD
        if getattr(mod, "node_group", None) != ng:
            mod.node_group = ng
    if reset_inputs:
        _set_modifier_socket(mod, "Height", float(height))
        _set_modifier_socket(mod, "Thickness", float(thickness))
    mod["ga_wall_thickness_signature"] = f"{float(thickness):.6f}/{float(height):.6f}"
    return mod


def remove_wall_gn_handlers():
    handlers = bpy.app.handlers.depsgraph_update_post
    for handler in list(handlers):
        if getattr(handler, "__name__", "") == "ga_wall_gn_update_handler":
            try:
                handlers.remove(handler)
            except ValueError:
                pass


def refresh_existing_wall_gn_modifiers(context):
    """Rebind and rebuild existing walls after reloading the add-on."""
    if not context or not getattr(context, "scene", None):
        return

    ng = get_or_create_wall_height_node_group()
    for obj in bpy.data.objects:
        if not obj or obj.type != 'MESH':
            continue
        mod = obj.modifiers.get(GA_WALL_GN_MOD)
        if not mod or mod.type != 'NODES':
            continue
        mod.node_group = ng
        height = float(obj.get("ga_wall_height", obj.get("gm_wall_height", 1.0)))
        thickness = float(obj.get("ga_wall_thickness", obj.get("gm_wall_thickness", 0.1)))
        if obj.get(WALL_KIND_KEY) == WALL_KIND_VAL and obj.get("ga_wall_edges"):
            side = obj.get("ga_wall_position") or gridarch_settings(context).wall_side_default
            rebuild_wall_object_from_props(context, obj, height, thickness, side)
        elif obj.get("ga_auto_wall"):
            source = bpy.data.objects.get(obj.get("ga_auto_wall_source", ""))
            me = build_auto_wall_mesh_from_plane(source, thickness)
            if me is not None:
                old = obj.data
                obj.data = me
                try:
                    if old and old.users == 0:
                        bpy.data.meshes.remove(old)
                except Exception:
                    pass
                add_or_get_wall_gn_modifier(obj, height=height, thickness=thickness)
        else:
            _set_modifier_socket(mod, "Height", height)
            _set_modifier_socket(mod, "Thickness", thickness)
            mod["ga_wall_thickness_signature"] = f"{thickness:.6f}/{height:.6f}"


def build_auto_wall_mesh_from_plane(src: bpy.types.Object, thickness: float) -> bpy.types.Mesh | None:
    if not src or src.type != 'MESH' or not src.data.polygons:
        return None

    bm = bmesh.new()
    bm.from_mesh(src.data)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    res = bmesh.ops.inset_region(
        bm,
        faces=list(bm.faces),
        thickness=max(float(thickness), 0.001),
        depth=0.0,
        use_even_offset=True,
        use_boundary=True,
        use_outset=False,
    )
    rim_faces = set(res.get('faces', []))
    if not rim_faces:
        bm.free()
        return None

    faces_to_delete = [f for f in bm.faces if f not in rim_faces]
    if faces_to_delete:
        bmesh.ops.delete(bm, geom=faces_to_delete, context='FACES')

    me_outer = bpy.data.meshes.new("GA_AutoWall_Mesh")
    bm.normal_update()
    bm.to_mesh(me_outer)
    bm.free()
    return me_outer


def get_or_create_collision_node_group(primitive: str = "CUBE") -> bpy.types.NodeTree:
    """Create a collision primitive GN group with editable X/Y/Z and Scale inputs."""
    primitive = primitive if primitive in {"CUBE", "SPHERE", "ICO_SPHERE", "CYLINDER", "CONE"} else "CUBE"
    name = f"GA_Collision_{primitive}_GN"
    ng = bpy.data.node_groups.get(name)
    if ng and ng.get("ga_cutter_gn_version") == 6:
        return ng

    if ng is None:
        ng = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    try:
        ng.interface.clear()
    except Exception:
        pass

    # Geometry input is included for modifier compatibility; output is generated.
    _new_group_socket(ng, "Geometry", 'INPUT', 'NodeSocketGeometry')
    _new_group_socket(ng, "X", 'INPUT', 'NodeSocketFloat')
    _new_group_socket(ng, "Y", 'INPUT', 'NodeSocketFloat')
    _new_group_socket(ng, "Z", 'INPUT', 'NodeSocketFloat')
    scale_socket = _new_group_socket(ng, "Scale", 'INPUT', 'NodeSocketFloat')
    _new_group_socket(ng, "Geometry", 'OUTPUT', 'NodeSocketGeometry')
    for socket, default in (
        (scale_socket, 1.0),
    ):
        try:
            socket.default_value = default
        except Exception:
            pass

    nodes = ng.nodes
    links = ng.links
    nodes.clear()

    group_in = nodes.new('NodeGroupInput')
    group_in.location = (-600, 0)

    combine = nodes.new('ShaderNodeCombineXYZ')
    combine.location = (-300, 40)

    scale_vec = nodes.new('ShaderNodeVectorMath')
    scale_vec.location = (-40, 40)
    try:
        scale_vec.operation = 'SCALE'
    except Exception:
        pass

    node_type = {
        "CUBE": 'GeometryNodeMeshCube',
        "SPHERE": 'GeometryNodeMeshUVSphere',
        "ICO_SPHERE": 'GeometryNodeMeshIcoSphere',
        "CYLINDER": 'GeometryNodeMeshCylinder',
        "CONE": 'GeometryNodeMeshCone',
    }[primitive]
    try:
        primitive_node = nodes.new(node_type)
    except Exception:
        primitive_node = nodes.new('GeometryNodeMeshCube')
        primitive = "CUBE"
    primitive_node.location = (190, 80)

    transform = nodes.new('GeometryNodeTransform')
    transform.location = (420, 40)

    try:
        if primitive == "CUBE":
            primitive_node.inputs["Size"].default_value = (1.0, 1.0, 1.0)
        elif primitive == "SPHERE":
            primitive_node.inputs["Radius"].default_value = 0.5
            for socket_name in ("Segments", "Rings"):
                if socket_name in primitive_node.inputs:
                    primitive_node.inputs[socket_name].default_value = 8
        elif primitive == "ICO_SPHERE":
            primitive_node.inputs["Radius"].default_value = 0.5
        elif primitive == "CYLINDER":
            primitive_node.inputs["Radius"].default_value = 0.5
            primitive_node.inputs["Depth"].default_value = 1.0
            if "Vertices" in primitive_node.inputs:
                primitive_node.inputs["Vertices"].default_value = 8
        elif primitive == "CONE":
            primitive_node.inputs["Radius Bottom"].default_value = 0.5
            primitive_node.inputs["Radius Top"].default_value = 0.0
            primitive_node.inputs["Depth"].default_value = 1.0
            if "Vertices" in primitive_node.inputs:
                primitive_node.inputs["Vertices"].default_value = 8
    except Exception:
        pass

    group_out = nodes.new('NodeGroupOutput')
    group_out.location = (680, 40)

    # Link sockets by name where possible. Socket names can vary slightly by Blender version,
    # so fall back to index-based access.
    def input_socket(node, *names, index=None):
        for n in names:
            if n in node.inputs:
                return node.inputs[n]
        return node.inputs[index] if index is not None else None

    def output_socket(node, *names, index=None):
        for n in names:
            if n in node.outputs:
                return node.outputs[n]
        return node.outputs[index] if index is not None else None

    links.new(output_socket(group_in, "X", index=1), input_socket(combine, "X", index=0))
    links.new(output_socket(group_in, "Y", index=2), input_socket(combine, "Y", index=1))
    links.new(output_socket(group_in, "Z", index=3), input_socket(combine, "Z", index=2))
    links.new(output_socket(combine, "Vector", index=0), input_socket(scale_vec, "Vector", index=0))
    links.new(output_socket(group_in, "Scale", index=4), input_socket(scale_vec, "Scale", index=3))
    links.new(output_socket(primitive_node, "Mesh", index=0), input_socket(transform, "Geometry", index=0))
    links.new(output_socket(scale_vec, "Vector", index=0), input_socket(transform, "Scale", index=3))
    links.new(output_socket(transform, "Geometry", index=0), input_socket(group_out, "Geometry", index=0))

    ng["ga_cutter_gn_version"] = 6
    ng["ga_collision_primitive"] = primitive
    return ng


def _set_nodes_modifier_input(mod: bpy.types.Modifier, socket_name: str, value: float):
    """Best-effort set of Geometry Nodes modifier input by interface socket name."""
    ng = getattr(mod, "node_group", None)
    if not ng:
        return

    # Blender 4/5 interface API.
    try:
        for item in ng.interface.items_tree:
            if getattr(item, "item_type", None) == 'SOCKET' and getattr(item, "name", "") == socket_name:
                identifier = getattr(item, "identifier", None)
                if identifier:
                    mod[identifier] = value
                    return
    except Exception:
        pass

    # Fallback: search modifier ID properties with matching name fragments.
    try:
        for key in mod.keys():
            if socket_name.lower() in str(key).lower():
                mod[key] = value
                return
    except Exception:
        pass


def add_cutter_cube_geometry_nodes(
    cutter: bpy.types.Object,
    x: float,
    y: float,
    z: float,
    primitive: str = "CUBE",
    scale: float = 1.0,
):
    """Attach GridArch cutter cube Geometry Nodes modifier and set X/Y/Z inputs."""
    if not cutter:
        return None

    ng = get_or_create_collision_node_group(primitive)
    mod = cutter.modifiers.new(name="GA_CutterCube_GN", type='NODES')
    mod.node_group = ng

    _set_nodes_modifier_input(mod, "X", x)
    _set_nodes_modifier_input(mod, "Y", y)
    _set_nodes_modifier_input(mod, "Z", z)
    _set_nodes_modifier_input(mod, "Scale", scale)
    return mod


class GA_OT_create_opening_cut(Operator):
    bl_idname = "ga.create_opening_cut"
    bl_label = "Opening Cut Mode"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        selected = [o for o in context.selected_objects if o and o.type == 'MESH']
        cutters = [o for o in selected if not is_wall_boolean_target(o)]
        if not cutters:
            self.report({'WARNING'}, "Select mesh object(s) to use as Boolean cutter source.")
            return {'CANCELLED'}

        targets = find_boolean_wall_targets(context, exclude=set(cutters))
        if not targets:
            self.report({'WARNING'}, "Select target Draw Wall/Auto Wall object(s) together with cutter source object(s).")
            return {'CANCELLED'}

        made = 0
        for source in cutters:
            cutter = self._create_cutter_from_source(context, source)
            if not cutter:
                continue
            for target in targets:
                self._add_boolean_to_target(target, cutter)
            made += 1

        self.report({'INFO'}, f"Created {made} boolean cutter(s)")
        return {'FINISHED'}

    def _create_cutter_from_source(self, context, source: bpy.types.Object):
        name_base = "GA_BCut"

        center = _world_bbox_center(source)
        size = _oriented_bbox_size(source)

        bpy.ops.mesh.primitive_cube_add(size=1.0, location=center)
        cutter = context.active_object
        cutter.name = f"{source.name}-{name_base}"

        # Match the selected source object's world rotation.
        # Do not apply rotation afterward; Geometry Nodes output must inherit this object transform.
        cutter.rotation_euler = source.matrix_world.to_euler()
        cutter.scale = (1.0, 1.0, 1.0)

        # Default cutter GN size follows the selected source object's oriented bounding box.
        bbox_x = max(float(size.x), 0.001)
        bbox_y = max(float(size.y), 0.001)
        bbox_z = max(float(size.z), 0.001)

        add_cutter_cube_geometry_nodes(cutter, bbox_x, bbox_y, bbox_z)

        cutter.display_type = 'WIRE'
        cutter.show_in_front = True
        cutter.hide_render = True
        cutter.hide_select = False
        tag_object(cutter, TAG_KEY, TAG_VAL)
        cutter["ga_opening_cut"] = "BOOLEAN"
        cutter["ga_source_object"] = source.name

        cutter.parent = source
        cutter.matrix_parent_inverse = source.matrix_world.inverted()
        return cutter

    def _add_boolean_to_target(self, target: bpy.types.Object, cutter: bpy.types.Object):
        mod_name = "GA_Boolean_Difference"
        mod = target.modifiers.new(name=mod_name, type='BOOLEAN')
        mod.operation = 'DIFFERENCE'
        mod.object = cutter
        try:
            mod.solver = 'EXACT'
        except Exception:
            pass
        cutter["ga_boolean_target"] = target.name
        return mod


# ----------------------------
# Settings
# ----------------------------


TAG_KEY = "_gridarch"
TAG_VAL = "placed"
Z_UNIT = 1.0  # Reserved for future integer layer support. Current GridArch is fixed to Z=0 only.


class GRIDARCH_Settings(PropertyGroup):
    grid_size: FloatProperty(
        name="Grid Size (m)",
        default=1.0,
        min=0.01,
        soft_max=10.0,
        description="XY grid size in meters. All placement uses this fixed grid.",
    )

    wall_side_default: EnumProperty(
        name="Wall Position (Default)",
        items=[
            ("LEFT", "Left", "Offset wall to the left side of the placement direction"),
            ("CENTER", "Center", "Place wall on the center line of placed markers"),
            ("RIGHT", "Right", "Offset wall to the right side of the placement direction"),
        ],
        default="CENTER",
        options={'HIDDEN'},
    )
    wall_thickness_default: FloatProperty(
        name="Wall Thickness (Default)",
        default=0.1,
        min=0.001,
        options={'HIDDEN'},
    )
    wall_height_default: FloatProperty(
        name="Wall Height (Default)",
        default=1.0,
        min=0.01,
        options={'HIDDEN'},
    )



# ----------------------------
# Placement Modal Operator
# ----------------------------


class VIEW3D_OT_gridmap3d_paint(Operator):
    bl_idname = "view3d.gridarch_paint"
    bl_label = "GridArch Tile"
    bl_options = {"REGISTER", "UNDO"}

    running: BoolProperty(default=False)

    def invoke(self, context, event):
        if context.area.type != "VIEW_3D":
            self.report({"WARNING"}, "Run this in a 3D View")
            return {"CANCELLED"}

        context.window_manager.modal_handler_add(self)
        self.running = True
        self._is_dragging = False
        self._is_erasing = False
        self._last_cell = None
        self._last_created = None
        _add_mode_overlay(self, "Tile Mode (GA)")
        try:
            context.area.tag_redraw()
        except Exception:
            pass
        return {"RUNNING_MODAL"}

    def _cell_from_mouse(self, context, event):
        s = gridarch_settings(context)
        ok, hit = raycast_to_ground(context, event, 0.0)
        if not ok:
            return None
        return (
            int(floor(hit.x / s.grid_size)),
            int(floor(hit.y / s.grid_size)),
            0,
        )

    def _place_at_cell(self, context, cell):
        s = gridarch_settings(context)
        self._last_created = ensure_grid_plane_object(context)
        add_grid_plane_tile(context, cell, float(s.grid_size))

    def _delete_at_cell(self, context, cell):
        s = gridarch_settings(context)
        removed = remove_grid_plane_cell_and_connected_line(context, cell, float(s.grid_size))
        if removed:
            self._last_created = ensure_grid_plane_object(context)
        return removed

    def modal(self, context, event):
        if not self.running:
            return {"CANCELLED"}

        if event.type in {"MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE"}:
            return {"PASS_THROUGH"}
        if event.type in {"LEFTMOUSE", "RIGHTMOUSE"} and event.alt:
            return {"PASS_THROUGH"}
        if event.type.startswith("NUMPAD_"):
            return {"PASS_THROUGH"}
        if event.type in {"Z", "ACCENT_GRAVE"}:
            return {"PASS_THROUGH"}
        if event.type in {"ZERO","ONE","TWO","THREE","FOUR","FIVE","SIX","SEVEN","EIGHT","NINE"}:
            return {"PASS_THROUGH"}

        if event.type == "ESC":
            try:
                bpy.ops.object.select_all(action='DESELECT')
            except Exception:
                pass

            if self._last_created and self._last_created.name in bpy.data.objects:
                try:
                    self._last_created.select_set(True)
                    context.view_layer.objects.active = self._last_created
                except Exception:
                    pass

            self.running = False
            self._is_dragging = False
            self._is_erasing = False
            self._last_cell = None
            _remove_mode_overlay(self)
            return {"CANCELLED"}

        if event.type == "RIGHTMOUSE" and event.value == "PRESS":
            self._is_erasing = True
            cell = self._cell_from_mouse(context, event)
            if cell:
                self._delete_at_cell(context, cell)
                self._last_cell = cell
            return {"RUNNING_MODAL"}

        if event.type == "RIGHTMOUSE" and event.value == "RELEASE":
            self._is_erasing = False
            self._last_cell = None
            self._last_marker_cell = None
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            self._is_dragging = True
            cell = self._cell_from_mouse(context, event)
            if cell:
                self._place_at_cell(context, cell)
                self._last_cell = cell
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            self._is_dragging = False
            self._last_cell = None
            return {"RUNNING_MODAL"}

        if event.type == "MOUSEMOVE":
            if self._is_dragging or self._is_erasing:
                cell = self._cell_from_mouse(context, event)
                if not cell:
                    return {"RUNNING_MODAL"}

                if self._last_cell is not None and cell == self._last_cell:
                    return {"RUNNING_MODAL"}

                if self._is_dragging:
                    self._place_at_cell(context, cell)
                else:
                    self._delete_at_cell(context, cell)

                self._last_cell = cell
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}


        return {"RUNNING_MODAL"}


# ----------------------------
# OrthogonalEdges (manual junction edge restore)
# ----------------------------


def _point_in_poly_xy(px: float, py: float, poly: list[Vector]) -> bool:
    """2D point-in-polygon test on face local XY."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i].x, poly[i].y
        xj, yj = poly[j].x, poly[j].y
        if (yi > py) != (yj > py):
            denom = yj - yi
            if abs(denom) > 1e-12:
                x_intersect = (xj - xi) * (py - yi) / denom + xi
                if px < x_intersect:
                    inside = not inside
        j = i
    return inside


def apply_orthogonal_edges_at_cell(context, cell: tuple[int, int, int], grid_size: float) -> bool:
    """Split the face covering cell so the clicked cell keeps all four edges.

    This avoids bbox-based reconstruction. Only cells that actually existed inside
    the original face are rebuilt, so L/T/+ empty corners are not filled.
    """
    obj = get_active_or_default_plane(context)
    if not obj or obj.type != 'MESH' or not obj.data.polygons:
        return False

    x, y, z = cell
    cell_minx = x * grid_size
    cell_maxx = (x + 1) * grid_size
    cell_miny = y * grid_size
    cell_maxy = (y + 1) * grid_size
    cell_cx = (cell_minx + cell_maxx) * 0.5
    cell_cy = (cell_miny + cell_maxy) * 0.5
    cell_z = z * Z_UNIT
    tol = 1e-4

    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)

    target = None
    target_cells = set()
    face_z = None

    for f in bm.faces:
        c = _plane_face_center_local(f)
        if abs(c.z - cell_z) >= tol:
            continue

        poly = [v.co.copy() for v in f.verts]
        if not _point_in_poly_xy(cell_cx, cell_cy, poly):
            continue

        minx, maxx, miny, maxy = _face_bbox_xy(f)
        ix0 = int(floor(minx / grid_size))
        ix1 = int(floor((maxx - tol) / grid_size))
        iy0 = int(floor(miny / grid_size))
        iy1 = int(floor((maxy - tol) / grid_size))

        for yy in range(iy0, iy1 + 1):
            for xx in range(ix0, ix1 + 1):
                px = (xx + 0.5) * grid_size
                py = (yy + 0.5) * grid_size
                if _point_in_poly_xy(px, py, poly):
                    target_cells.add((xx, yy))

        target = f
        face_z = c.z
        break

    if not target or face_z is None or not target_cells:
        bm.free()
        return False

    if target_cells == {(x, y)}:
        bm.free()
        return True

    bmesh.ops.delete(bm, geom=[target], context='FACES')

    remaining = set(target_cells)
    remaining.discard((x, y))

    # Clicked cell is rebuilt as an independent 1x1 square so all four edges exist.
    _add_plane_rect_face(bm, cell_minx, cell_maxx, cell_miny, cell_maxy, face_z)

    # Rebuild the rest of the original footprint only, using greedy rectangles.
    while remaining:
        rx0, ry0 = min(remaining, key=lambda p: (p[1], p[0]))
        rx1 = rx0
        while (rx1 + 1, ry0) in remaining:
            rx1 += 1

        ry1 = ry0
        while True:
            ny = ry1 + 1
            if all((xx, ny) in remaining for xx in range(rx0, rx1 + 1)):
                ry1 = ny
            else:
                break

        for yy in range(ry0, ry1 + 1):
            for xx in range(rx0, rx1 + 1):
                remaining.discard((xx, yy))

        _add_plane_rect_face(
            bm,
            rx0 * grid_size,
            (rx1 + 1) * grid_size,
            ry0 * grid_size,
            (ry1 + 1) * grid_size,
            face_z
        )

    # Join vertices after restoring the orthogonal edges so the tile remains part of the mesh.
    # Height dragging searches by XY footprint, so it can still keep tracking raised cells.
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    me.update()
    return True


def _rasterize_face_cells(face: bmesh.types.BMFace, grid_size: float, tol: float = 1e-4) -> set[tuple[int, int]]:
    poly = [v.co.copy() for v in face.verts]
    minx, maxx, miny, maxy = _face_bbox_xy(face)
    ix0 = int(floor(minx / grid_size))
    ix1 = int(floor((maxx - tol) / grid_size))
    iy0 = int(floor(miny / grid_size))
    iy1 = int(floor((maxy - tol) / grid_size))
    cells = set()
    for yy in range(iy0, iy1 + 1):
        for xx in range(ix0, ix1 + 1):
            px = (xx + 0.5) * grid_size
            py = (yy + 0.5) * grid_size
            if _point_in_poly_xy(px, py, poly):
                cells.add((xx, yy))
    return cells


def _find_face_covering_cell(bm: bmesh.types.BMesh, cell: tuple[int, int, int], grid_size: float, tol: float = 1e-4, ignore_z: bool = False):
    x, y, z = cell
    cx = (x + 0.5) * grid_size
    cy = (y + 0.5) * grid_size
    cz = z * Z_UNIT
    best = None
    best_score = None
    for f in bm.faces:
        c = _plane_face_center_local(f)
        if not ignore_z and abs(c.z - cz) >= tol:
            continue

        poly = [v.co.copy() for v in f.verts]
        minx, maxx, miny, maxy = _face_bbox_xy(f)
        if not ((minx - tol) <= cx <= (maxx + tol) and (miny - tol) <= cy <= (maxy + tol)):
            continue
        if not _point_in_poly_xy(cx, cy, poly):
            continue

        area = max(1e-9, (maxx - minx) * (maxy - miny))
        # Prefer the smallest footprint, then closest height. This helps after splitting/raising.
        z_penalty = abs(c.z - cz) if not ignore_z else 0.0
        score = area + z_penalty * 0.001
        if best is None or score < best_score:
            best = f
            best_score = score
    return best


def set_orthogonal_cell_height(context, cell: tuple[int, int, int], grid_size: float, new_z: float) -> bool:
    obj = get_active_or_default_plane(context)
    if not obj or obj.type != 'MESH' or not obj.data.polygons:
        return False

    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    f = _find_face_covering_cell(bm, cell, grid_size, ignore_z=True)
    if not f:
        bm.free()
        return False

    # Prefer exact 1x1 face. If the cell is still part of a larger rectangle,
    # split first so height editing does not affect the whole rectangle.
    x, y, _z = cell
    minx, maxx, miny, maxy = _face_bbox_xy(f)
    if not (
        abs(minx - x * grid_size) < 1e-4 and
        abs(maxx - (x + 1) * grid_size) < 1e-4 and
        abs(miny - y * grid_size) < 1e-4 and
        abs(maxy - (y + 1) * grid_size) < 1e-4
    ):
        bm.free()
        apply_orthogonal_edges_at_cell(context, cell, grid_size)
        bm = bmesh.new()
        bm.from_mesh(me)
        f = _find_face_covering_cell(bm, cell, grid_size, ignore_z=True)
        if not f:
            bm.free()
            return False

    for v in f.verts:
        v.co.z = float(new_z)

    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    me.update()
    return True


def get_orthogonal_cell_height(context, cell: tuple[int, int, int], grid_size: float) -> float | None:
    obj = get_active_or_default_plane(context)
    if not obj or obj.type != 'MESH' or not obj.data.polygons:
        return None
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    f = _find_face_covering_cell(bm, cell, grid_size, ignore_z=True)
    if not f:
        bm.free()
        return None
    z = _plane_face_center_local(f).z
    bm.free()
    return float(z)


def _orthogonal_cell_from_mouse(context, event):
    """Pick a cell for OrthogonalEdges using the actual GridArch_Plane mesh first.

    This is more reliable than projecting to Z=0, especially after OrthogonalEdges
    has split or raised individual tiles. Falls back to the Z=0 grid plane only
    when the mesh raycast misses.
    """
    s = gridarch_settings(context)
    obj = get_active_or_default_plane(context)

    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None:
        return None

    coord = (event.mouse_region_x, event.mouse_region_y)
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)

    if obj and obj.type == 'MESH' and obj.data.polygons:
        inv = obj.matrix_world.inverted()
        origin_l = inv @ origin
        direction_l = (inv.to_3x3() @ direction).normalized()
        hit, loc_l, _normal_l, _face_index = obj.ray_cast(origin_l, direction_l)
        if hit:
            return (
                int(floor(loc_l.x / s.grid_size)),
                int(floor(loc_l.y / s.grid_size)),
                0,
            )

    ok, hit = raycast_to_ground(context, event, 0.0)
    if not ok:
        return None
    return (
        int(floor(hit.x / s.grid_size)),
        int(floor(hit.y / s.grid_size)),
        0,
    )


def collapse_orthogonal_edges_at_cell(context, cell: tuple[int, int, int], grid_size: float) -> bool:
    """Remove internal orthogonal edges around the clicked cell.

    RMB collapse always resets the rebuilt tile area to the default plane height
    for the cell layer. In current GridArch this means Z=0.
    """
    obj = get_active_or_default_plane(context)
    if not obj or obj.type != 'MESH' or not obj.data.polygons:
        return False

    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    target = _find_face_covering_cell(bm, cell, grid_size, ignore_z=True)
    if not target:
        bm.free()
        return False

    # Reset collapsed/rebuilt area to the default layer height.
    target_z = cell[2] * Z_UNIT
    tx, ty, _tz = cell
    cell_minx = tx * grid_size
    cell_maxx = (tx + 1) * grid_size
    cell_miny = ty * grid_size
    cell_maxy = (ty + 1) * grid_size
    tol = 1e-4

    faces_to_merge = []
    cells = set()

    for f in list(bm.faces):
        # Intentionally ignore current face height here.
        # Raised tiles must be allowed to merge back and reset to default height.
        minx, maxx, miny, maxy = _face_bbox_xy(f)

        touches_or_contains = (
            ((minx - tol) <= (tx + 0.5) * grid_size <= (maxx + tol) and (miny - tol) <= (ty + 0.5) * grid_size <= (maxy + tol)) or
            (abs(maxx - cell_minx) < tol and max(miny, cell_miny) < min(maxy, cell_maxy) - tol) or
            (abs(minx - cell_maxx) < tol and max(miny, cell_miny) < min(maxy, cell_maxy) - tol) or
            (abs(maxy - cell_miny) < tol and max(minx, cell_minx) < min(maxx, cell_maxx) - tol) or
            (abs(miny - cell_maxy) < tol and max(minx, cell_minx) < min(maxx, cell_maxx) - tol)
        )
        if not touches_or_contains:
            continue

        faces_to_merge.append(f)
        cells.update(_rasterize_face_cells(f, grid_size, tol))

    if not faces_to_merge or not cells:
        bm.free()
        return False

    bmesh.ops.delete(bm, geom=faces_to_merge, context='FACES')

    remaining = set(cells)
    while remaining:
        rx0, ry0 = min(remaining, key=lambda p: (p[1], p[0]))
        rx1 = rx0
        while (rx1 + 1, ry0) in remaining:
            rx1 += 1
        ry1 = ry0
        while True:
            ny = ry1 + 1
            if all((xx, ny) in remaining for xx in range(rx0, rx1 + 1)):
                ry1 = ny
            else:
                break
        for yy in range(ry0, ry1 + 1):
            for xx in range(rx0, rx1 + 1):
                remaining.discard((xx, yy))
        _add_plane_rect_face(bm, rx0 * grid_size, (rx1 + 1) * grid_size, ry0 * grid_size, (ry1 + 1) * grid_size, target_z)

    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    me.update()
    return True


class VIEW3D_OT_gridarch_orthogonal_edges(Operator):
    bl_idname = "view3d.gridarch_orthogonal_edges"
    bl_label = "Manual Optimize"
    bl_options = {"REGISTER", "UNDO"}

    running: BoolProperty(default=False, options={'HIDDEN'})

    def invoke(self, context, event):
        if context.area.type != "VIEW_3D":
            self.report({"WARNING"}, "Run this in a 3D View")
            return {"CANCELLED"}
        self.running = True
        self._height_dragging = False
        self._height_cell = None
        self._height_start_mouse_y = 0
        self._height_base_z = 0.0
        self._lmb_dragging = False
        self._rmb_dragging = False
        self._last_manual_cell = None
        context.window_manager.modal_handler_add(self)
        _add_mode_overlay(self, "Manual Optimize Mode (GA)")
        try:
            context.area.tag_redraw()
        except Exception:
            pass
        return {"RUNNING_MODAL"}

    def _cell_from_mouse(self, context, event):
        return _orthogonal_cell_from_mouse(context, event)

    def modal(self, context, event):
        if not self.running:
            return {"CANCELLED"}

        if event.type in {"MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE"}:
            return {"PASS_THROUGH"}
        if event.type in {"LEFTMOUSE", "RIGHTMOUSE"} and event.alt:
            return {"PASS_THROUGH"}
        if event.type.startswith("NUMPAD_"):
            return {"PASS_THROUGH"}

        if event.type == "ESC":
            self.running = False
            self._height_dragging = False
            self._height_cell = None
            self._lmb_dragging = False
            self._rmb_dragging = False
            self._last_manual_cell = None
            return {"CANCELLED"}

        if event.type == "RIGHTMOUSE" and event.value == "PRESS":
            s = gridarch_settings(context)
            cell = self._cell_from_mouse(context, event)
            self._rmb_dragging = True
            self._last_manual_cell = cell
            if cell:
                if event.ctrl:
                    set_orthogonal_cell_height(context, cell, float(s.grid_size), 0.0)
                else:
                    apply_orthogonal_edges_at_cell(context, cell, float(s.grid_size))
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            self._height_dragging = False
            self._height_cell = None
            self._lmb_dragging = False
            self._last_manual_cell = None
            return {"RUNNING_MODAL"}

        if event.type == "RIGHTMOUSE" and event.value == "RELEASE":
            self._rmb_dragging = False
            self._last_manual_cell = None
            return {"RUNNING_MODAL"}

        if self._height_dragging and event.type == "MOUSEMOVE":
            s = gridarch_settings(context)
            dy = event.mouse_region_y - self._height_start_mouse_y
            steps = int(round(dy / 24.0))
            new_z = self._height_base_z + steps * 0.25
            set_orthogonal_cell_height(context, self._height_cell, float(s.grid_size), new_z)
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}

        if event.type == "MOUSEMOVE":
            s = gridarch_settings(context)
            cell = self._cell_from_mouse(context, event)
            if not cell or cell == self._last_manual_cell:
                return {"RUNNING_MODAL"}

            if self._lmb_dragging:
                if event.ctrl:
                    base_z = get_orthogonal_cell_height(context, cell, float(s.grid_size))
                    if base_z is not None:
                        self._height_dragging = True
                        self._height_cell = cell
                        self._height_start_mouse_y = event.mouse_region_y
                        self._height_base_z = base_z
                else:
                    collapse_orthogonal_edges_at_cell(context, cell, float(s.grid_size))
                self._last_manual_cell = cell
                context.area.tag_redraw()
                return {"RUNNING_MODAL"}

            if self._rmb_dragging:
                if event.ctrl:
                    set_orthogonal_cell_height(context, cell, float(s.grid_size), 0.0)
                else:
                    apply_orthogonal_edges_at_cell(context, cell, float(s.grid_size))
                self._last_manual_cell = cell
                context.area.tag_redraw()
                return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            s = gridarch_settings(context)
            cell = self._cell_from_mouse(context, event)
            self._lmb_dragging = True
            self._last_manual_cell = cell
            if cell:
                if event.ctrl:
                    base_z = get_orthogonal_cell_height(context, cell, float(s.grid_size))
                    if base_z is not None:
                        self._height_dragging = True
                        self._height_cell = cell
                        self._height_start_mouse_y = event.mouse_region_y
                        self._height_base_z = base_z
                else:
                    ok = collapse_orthogonal_edges_at_cell(context, cell, float(s.grid_size))
                    if not ok:
                        self.report({'INFO'}, 'No internal orthogonal edge found at clicked cell')
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}

        return {"RUNNING_MODAL"}


# ----------------------------
# Create Wall (modal)
# ----------------------------

GRID_WALL_NAME = "GA_DrawWall"
WALL_KIND_KEY = "ga_kind"
WALL_KIND_VAL = "WALL"


def _create_new_grid_wall_object(context) -> bpy.types.Object:
    """Always create a NEW combined draw wall object."""
    me = bpy.data.meshes.new(GRID_WALL_NAME + "_Mesh")
    obj = bpy.data.objects.new(GRID_WALL_NAME, me)  # Blender auto-uniques names
    scene_collection(context).objects.link(obj)

    tag_object(obj, TAG_KEY, TAG_VAL)
    obj[WALL_KIND_KEY] = WALL_KIND_VAL

    obj.location = Vector((0.0, 0.0, 0.0))

    return obj


def _get_wall_object_by_name(name: str) -> bpy.types.Object | None:
    if not name:
        return None
    obj = bpy.data.objects.get(name)
    if obj and obj.type == 'MESH' and is_tagged(obj, TAG_KEY, TAG_VAL) and obj.get(WALL_KIND_KEY) == WALL_KIND_VAL:
        return obj
    return None


def ensure_grid_wall_object(context, wall_obj_name: str = "") -> bpy.types.Object:
    """Get the operator-owned wall object if provided, otherwise create a new one."""
    owned = _get_wall_object_by_name(wall_obj_name)
    if owned:
        return owned
    return _create_new_grid_wall_object(context)


def _parse_cells_string(s: str) -> list[tuple[int, int, int]]:
    cells = []
    for part in [p for p in (s or "").split(';') if p]:
        try:
            x, y, z = part.split(',')
            cells.append((int(x), int(y), int(z)))
        except Exception:
            pass
    return cells


def _parse_edges_string(s: str) -> list[tuple[tuple[int, int, int], tuple[int, int, int]]]:
    edges = []
    for part in [p for p in (s or "").split(';') if p]:
        try:
            a, b = part.split('>')
            ax, ay, az = a.split(',')
            bx, by, bz = b.split(',')
            edges.append(((int(ax), int(ay), int(az)), (int(bx), int(by), int(bz))))
        except Exception:
            pass
    return edges


def _edge_to_string(a: tuple[int, int, int], b: tuple[int, int, int]) -> str:
    return f"{a[0]},{a[1]},{a[2]}>{b[0]},{b[1]},{b[2]}"


def _cells_are_cardinal_neighbors(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    return a[2] == b[2] and abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1


def _wall_global_offset_from_edges(edges, position: str, grid: float) -> Vector:
    """Return one shared LEFT/RIGHT offset vector for the whole wall object."""
    if position not in {"LEFT", "RIGHT"} or not edges:
        return Vector((0.0, 0.0, 0.0))

    a, b = sorted(edges, key=lambda e: (e[0], e[1]))[0]
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    tangent = Vector((float(dx), float(dy), 0.0))
    if tangent.length < 1e-8:
        return Vector((0.0, 0.0, 0.0))
    tangent.normalize()
    normal = Vector((-tangent.y, tangent.x, 0.0))
    sign = 1.0 if position == "LEFT" else -1.0
    return normal * (grid * 0.5 * sign)


def _merge_wall_edges_to_runs(
    edges: list[tuple[tuple[int, int, int], tuple[int, int, int]]],
    grid: float,
    thickness: float,
):
    """Build rectangular wall runs split at corners and branch points.

    Endpoints are extended to the tile boundary but clipped so that the
    wall thickness does not push geometry beyond the tile edge. The
    effective endpoint extension is (grid * 0.5 - half_thickness),
    never negative.
    """
    half_t = max(float(thickness), 0.001) * 0.5
    # extend endpoints to reach tile border but avoid penetrating adjacent
    # outer geometry by subtracting half the wall thickness
    end_extend = max(float(grid) * 0.5 - half_t, 0.0)
    degrees = _wall_vertex_degrees(edges)

    edge_data: dict[tuple[tuple[int, int, int], tuple[int, int, int]], tuple[tuple[int, int, int], tuple[int, int, int], str]] = {}
    vertex_edges: dict[tuple[int, int, int], list[tuple[tuple[tuple[int, int, int], tuple[int, int, int]], tuple[int, int, int]]]] = defaultdict(list)

    for a, b in edges:
        if a[2] != b[2]:
            continue
        if a[1] == b[1] and abs(a[0] - b[0]) == 1:
            orientation = "H"
        elif a[0] == b[0] and abs(a[1] - b[1]) == 1:
            orientation = "V"
        else:
            continue
        edge_key = (a, b)
        edge_data[edge_key] = (a, b, orientation)
        vertex_edges[a].append((edge_key, b))
        vertex_edges[b].append((edge_key, a))

    used_edges = set()
    rects = []

    for edge_key, (a, b, orientation) in edge_data.items():
        if edge_key in used_edges:
            continue

        start = a
        end = b
        used_edges.add(edge_key)

        current = a
        prev_vertex = b
        while True:
            candidates = [
                (candidate_key, other_vertex)
                for candidate_key, other_vertex in vertex_edges.get(current, [])
                if candidate_key not in used_edges
                and edge_data.get(candidate_key, (None, None, ""))[2] == orientation
            ]
            if len(candidates) != 1:
                break
            candidate_key, other_vertex = candidates[0]
            if other_vertex == prev_vertex:
                break
            used_edges.add(candidate_key)
            start = other_vertex
            prev_vertex = current
            current = other_vertex

        current = b
        prev_vertex = a
        while True:
            candidates = [
                (candidate_key, other_vertex)
                for candidate_key, other_vertex in vertex_edges.get(current, [])
                if candidate_key not in used_edges
                and edge_data.get(candidate_key, (None, None, ""))[2] == orientation
            ]
            if len(candidates) != 1:
                break
            candidate_key, other_vertex = candidates[0]
            if other_vertex == prev_vertex:
                break
            used_edges.add(candidate_key)
            end = other_vertex
            prev_vertex = current
            current = other_vertex

        if orientation == "H":
            left = start if start[0] <= end[0] else end
            right = end if start[0] <= end[0] else start
            x0 = (left[0] + 0.5) * grid
            x1 = (right[0] + 0.5) * grid
            if degrees.get(left, 0) == 1:
                x0 -= end_extend
            if degrees.get(right, 0) == 1:
                x1 += end_extend
            y = (left[1] + 0.5) * grid
            rects.append((x0 - half_t, x1 + half_t, y - half_t, y + half_t))
        else:
            bottom = start if start[1] <= end[1] else end
            top = end if start[1] <= end[1] else start
            x = (bottom[0] + 0.5) * grid
            y0 = (bottom[1] + 0.5) * grid
            y1 = (top[1] + 0.5) * grid
            if degrees.get(bottom, 0) == 1:
                y0 -= end_extend
            if degrees.get(top, 0) == 1:
                y1 += end_extend
            rects.append((x - half_t, x + half_t, y0 - half_t, y1 + half_t))

    return rects


def _wall_vertex_degrees(edges: list[tuple[tuple[int, int, int], tuple[int, int, int]]]) -> dict[tuple[int, int, int], int]:
    degrees = defaultdict(int)
    for a, b in edges:
        degrees[a] += 1
        degrees[b] += 1
    return degrees


def build_wall_footprint_mesh_from_edges(
    wall_obj: bpy.types.Object,
    edges: list[tuple[tuple[int, int, int], tuple[int, int, int]]],
    grid: float,
    thickness: float,
    global_offset: Vector | None = None,
) -> int:
    """Build the horizontal wall footprint mesh. GN handles vertical extrusion."""
    rects = _merge_wall_edges_to_runs(edges, grid, thickness)

    if global_offset is not None:
        dx = float(global_offset.x)
        dy = float(global_offset.y)
        rects = [(x0 + dx, x1 + dx, y0 + dy, y1 + dy) for x0, x1, y0, y1 in rects]

    if not rects:
        return 0

    xs = sorted({x for rect in rects for x in (rect[0], rect[1])})
    ys = sorted({y for rect in rects for y in (rect[2], rect[3])})
    occupied = []

    for ix in range(len(xs) - 1):
        for iy in range(len(ys) - 1):
            cell_x0 = xs[ix]
            cell_x1 = xs[ix + 1]
            cell_y0 = ys[iy]
            cell_y1 = ys[iy + 1]
            for x0, x1, y0, y1 in rects:
                if not (cell_x1 <= x0 or cell_x0 >= x1 or cell_y1 <= y0 or cell_y0 >= y1):
                    occupied.append((ix, iy))
                    break

    if not occupied:
        return 0

    me = wall_obj.data
    bm = bmesh.new()
    verts = {}
    for ix, iy in occupied:
        x0, x1 = xs[ix], xs[ix + 1]
        y0, y1 = ys[iy], ys[iy + 1]
        coords = (
            (x0, y0, 0.0),
            (x1, y0, 0.0),
            (x1, y1, 0.0),
            (x0, y1, 0.0),
        )
        face_verts = []
        for co in coords:
            vert = verts.get(co)
            if vert is None:
                vert = bm.verts.new(co)
                verts[co] = vert
            face_verts.append(vert)
        try:
            bm.faces.new(face_verts)
        except ValueError:
            pass

    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    me.update()
    return len(occupied)


def rebuild_wall_object_from_props(context, wall_obj: bpy.types.Object, height: float, thickness: float, side: str, reset_inputs: bool = True):
    """Rebuild a wall object's mesh using stored directed edges or legacy cell list."""
    if not wall_obj or wall_obj.type != 'MESH':
        return

    s = gridarch_settings(context)
    grid = float(s.grid_size)

    edges = _parse_edges_string(wall_obj.get("ga_wall_edges", ""))

    if not edges:
        return

    me = wall_obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    if bm.verts or bm.edges or bm.faces:
        bmesh.ops.delete(bm, geom=list(bm.verts) + list(bm.edges) + list(bm.faces), context='VERTS')
    bm.to_mesh(me)
    bm.free()
    me.update()

    global_offset = _wall_global_offset_from_edges(edges, side, grid)
    build_wall_footprint_mesh_from_edges(wall_obj, edges, grid, float(thickness), global_offset=global_offset)
    if wall_obj.data.polygons:
        add_or_get_wall_gn_modifier(wall_obj, height=float(height), thickness=float(thickness), reset_inputs=reset_inputs)

    wall_obj["ga_wall_position"] = side
    wall_obj["ga_wall_height"] = float(height)
    wall_obj["ga_wall_thickness"] = float(thickness)



GA_WALL_GN_REBUILDING = False


def _wall_gn_thickness_signature(mod: bpy.types.Modifier) -> str:
    thickness = float(_get_modifier_socket(mod, "Thickness", 0.1) or 0.1)
    height = float(_get_modifier_socket(mod, "Height", 1.0) or 1.0)
    return f"{thickness:.6f}/{height:.6f}"


def _wall_height_from_modifier(mod: bpy.types.Modifier, default=1.0) -> float:
    value = _get_modifier_socket(mod, "Height", default)
    try:
        if isinstance(value, (list, tuple, Vector)):
            return abs(float(value[2]))
        return float(value)
    except Exception:
        return float(default)


def _sync_wall_from_gn_modifier(context, obj: bpy.types.Object, mod: bpy.types.Modifier):
    height = max(_wall_height_from_modifier(mod, obj.get("ga_wall_height", 1.0)), 0.001)
    thickness = max(float(_get_modifier_socket(mod, "Thickness", obj.get("ga_wall_thickness", obj.get("gm_wall_thickness", 0.1))) or 0.1), 0.001)

    if obj.get(WALL_KIND_KEY) == WALL_KIND_VAL:
        side = obj.get("ga_wall_position") or gridarch_settings(context).wall_side_default
        rebuild_wall_object_from_props(context, obj, height, thickness, side, reset_inputs=False)
    elif obj.get("ga_auto_wall"):
        source = bpy.data.objects.get(obj.get("ga_auto_wall_source", ""))
        me = build_auto_wall_mesh_from_plane(source, thickness)
        if me is not None:
            old = obj.data
            obj.data = me
            obj.update_tag(refresh={'DATA'})
            try:
                if old and old.users == 0:
                    bpy.data.meshes.remove(old)
            except Exception:
                pass

    obj["gm_wall_thickness"] = thickness
    obj["gm_wall_height"] = height
    obj["ga_wall_height"] = height
    obj["ga_wall_thickness"] = thickness
    mod["ga_wall_thickness_signature"] = f"{thickness:.6f}/{height:.6f}"


@persistent
def ga_wall_gn_update_handler(scene, depsgraph):
    global GA_WALL_GN_REBUILDING
    if GA_WALL_GN_REBUILDING:
        return

    context = bpy.context
    GA_WALL_GN_REBUILDING = True
    try:
        for obj in scene.objects:
            if not obj or obj.type != 'MESH':
                continue
            mod = obj.modifiers.get(GA_WALL_GN_MOD)
            if not mod or mod.type != 'NODES':
                continue
            sig = _wall_gn_thickness_signature(mod)
            if sig == mod.get("ga_wall_thickness_signature"):
                continue
            _sync_wall_from_gn_modifier(context, obj, mod)
            try:
                context.view_layer.update()
            except Exception:
                pass
    finally:
        GA_WALL_GN_REBUILDING = False


class VIEW3D_OT_gridmap3d_wall_mode(Operator):
    bl_idname = "view3d.gridarch_wall_mode"
    bl_label = "Create Draw Wall"
    bl_options = {"REGISTER", "UNDO"}

    # Shown in Adjust Last Operation (redo panel) AFTER exiting modal
    wall_side: EnumProperty(
        name="Wall Position",
        items=[
            ("LEFT", "Left", "Offset wall to the left side of the placement direction"),
            ("CENTER", "Center", "Place wall on the center line of placed markers"),
            ("RIGHT", "Right", "Offset wall to the right side of the placement direction"),
        ],
        default="CENTER",
    )
    wall_thickness: FloatProperty(name="Thickness (m)", default=0.1, min=0.001)
    wall_height: FloatProperty(name="Height (m)", default=1.0, min=0.01)

    # Hidden: stores placed cells as a semicolon-separated list: "x,y,z;x,y,z;..."
    placed_cells: StringProperty(default="", options={'HIDDEN'})

    # Hidden: wall object created by THIS operator instance (for redo, without overwriting older walls)
    wall_obj_name: StringProperty(default="", options={'HIDDEN'})

    running: BoolProperty(default=False, options={'HIDDEN'})

    def invoke(self, context, event):
        if context.area.type != "VIEW_3D":
            self.report({"WARNING"}, "Run this in a 3D View")
            return {"CANCELLED"}

        # Load hidden defaults from Scene
        s = gridarch_settings(context)
        try:
            self.wall_side = s.wall_side_default
            self.wall_thickness = float(s.wall_thickness_default)
            self.wall_height = float(s.wall_height_default)
        except Exception as ex:
            self.report({"WARNING"}, f"Failed to load wall defaults: {ex}")

        self.placed_cells = ""
        self.wall_obj_name = ""  # new run -> create a new wall object on first execute
        self.running = True
        self._is_dragging = False
        self._is_erasing = False
        self._last_cell = None
        self._last_marker_cell = None
        self._placed_cell_keys = set()
        self._wall_edges = set()

        context.window_manager.modal_handler_add(self)
        _add_mode_overlay(self, "Wall Mode (GA)")
        try:
            context.area.tag_redraw()
        except Exception:
            pass
        return {"RUNNING_MODAL"}

    # ---- marker blocks (temporary)
    def _marker_name(self, cell: tuple[int, int, int]) -> str:
        return f"GA_DrawWallMarker_{cell[0]}_{cell[1]}_{cell[2]}"

    def _find_marker(self, cell: tuple[int, int, int]):
        return bpy.data.objects.get(self._marker_name(cell))

    def _place_marker(self, context, cell: tuple[int, int, int]):
        s = gridarch_settings(context)
        name = self._marker_name(cell)

        # Fixed preview block size. It represents a marker only, not final wall thickness.
        marker_size = 0.25
        if not bpy.data.objects.get(name):
            cx = (cell[0] + 0.5) * float(s.grid_size)
            cy = (cell[1] + 0.5) * float(s.grid_size)
            cz = cell[2] * float(Z_UNIT)
            loc = Vector((cx, cy, cz + marker_size * 0.5))

            bpy.ops.mesh.primitive_cube_add(size=marker_size, location=loc)
            marker = context.active_object
            marker.name = name
            tag_object(marker, TAG_KEY, TAG_VAL)
            marker[WALL_KIND_KEY] = "WALL_MARKER"

        key = f"{cell[0]},{cell[1]},{cell[2]}"
        if key not in self._placed_cell_keys:
            self._placed_cell_keys.add(key)
            self.placed_cells = (self.placed_cells + ";" + key) if self.placed_cells else key

        # Build wall edges from spatial adjacency, not placement order.
        # This fixes L/T/+ creation when the corner/cross marker is placed last.
        # Direction is normalized for stable LEFT/RIGHT behavior:
        # - horizontal: west -> east
        # - vertical: south -> north
        for other_key in list(self._placed_cell_keys):
            if other_key == key:
                continue
            try:
                ox, oy, oz = (int(v) for v in other_key.split(','))
            except Exception:
                continue
            other = (ox, oy, oz)
            if not _cells_are_cardinal_neighbors(other, cell):
                continue

            if other[0] != cell[0]:
                a, b = (other, cell) if other[0] < cell[0] else (cell, other)
            else:
                a, b = (other, cell) if other[1] < cell[1] else (cell, other)

            edge_key = _edge_to_string(a, b)
            self._wall_edges.add(edge_key)

        self._last_marker_cell = cell

    def _erase_marker(self, context, cell: tuple[int, int, int]):
        obj = self._find_marker(cell)
        if not obj:
            return
        bpy.data.objects.remove(obj, do_unlink=True)

        # Remove from list
        key = f"{cell[0]},{cell[1]},{cell[2]}"
        parts = [p for p in self.placed_cells.split(';') if p and p != key]
        self.placed_cells = ';'.join(parts)
        if hasattr(self, '_placed_cell_keys'):
            self._placed_cell_keys.discard(key)

        # Remove any edges connected to this marker.
        if hasattr(self, '_wall_edges'):
            prefix = key + ">"
            suffix = ">" + key
            self._wall_edges = {e for e in self._wall_edges if not (e.startswith(prefix) or e.endswith(suffix))}

        if getattr(self, '_last_marker_cell', None) == cell:
            self._last_marker_cell = None

    # ---- picking
    def _cell_from_mouse(self, context, event):
        s = gridarch_settings(context)
        ok, hit = raycast_to_ground(context, event, 0.0)
        if not ok:
            return None

        # Z is always fixed to integer layer 0
        return (
            int(floor(hit.x / s.grid_size)),
            int(floor(hit.y / s.grid_size)),
            0,
        )

    # ---- conversion
    def execute(self, context):
        """Convert placed markers into wall quads on a shared Draw Wall object.

        This is called:
        - once when exiting modal (ESC)
        - again when the user adjusts properties in the redo panel
        """
        s = gridarch_settings(context)

        # Persist defaults for next run
        try:
            s.wall_side_default = self.wall_side
            s.wall_thickness_default = float(self.wall_thickness)
            s.wall_height_default = float(self.wall_height)
        except Exception as ex:
            self.report({"WARNING"}, f"Failed to save wall defaults: {ex}")

        cells = _parse_cells_string(self.placed_cells)

        # Get (or create) the wall object for THIS run. This prevents overwriting older walls.
        wall_obj = ensure_grid_wall_object(context, wall_obj_name=self.wall_obj_name)
        # Store the created object's name so redo edits rebuild the same one.
        self.wall_obj_name = wall_obj.name

        # wipe geometry
        me = wall_obj.data
        bm = bmesh.new()
        bm.from_mesh(me)
        bmesh.ops.delete(bm, geom=list(bm.verts) + list(bm.edges) + list(bm.faces), context='VERTS')
        bm.to_mesh(me)
        bm.free()
        me.update()

        # Rebuild from directed marker edges.
        edges = _parse_edges_string(';'.join(sorted(getattr(self, '_wall_edges', set()))))
        global_offset = _wall_global_offset_from_edges(edges, self.wall_side, float(s.grid_size))
        build_wall_footprint_mesh_from_edges(
            wall_obj,
            edges,
            float(s.grid_size),
            float(self.wall_thickness),
            global_offset=global_offset,
        )
        if wall_obj.data.polygons:
            add_or_get_wall_gn_modifier(wall_obj, height=float(self.wall_height), thickness=float(self.wall_thickness))

        # Store cells + params on the wall object so height can be changed later.
        wall_obj["ga_wall_edges"] = ';'.join(sorted(getattr(self, '_wall_edges', set())))
        wall_obj["ga_wall_position"] = self.wall_side
        wall_obj["ga_wall_height"] = float(self.wall_height)
        wall_obj["ga_wall_thickness"] = float(self.wall_thickness)

        # Remove markers after conversion
        for cell in cells:
            obj = bpy.data.objects.get(self._marker_name(cell))
            if obj:
                bpy.data.objects.remove(obj, do_unlink=True)

        # Select resulting wall object
        try:
            bpy.ops.object.select_all(action='DESELECT')
        except Exception:
            pass
        try:
            wall_obj.select_set(True)
            context.view_layer.objects.active = wall_obj
        except Exception:
            pass

        return {'FINISHED'}

    def modal(self, context, event):
        if not self.running:
            return {"CANCELLED"}

        # Let normal navigation happen.
        if event.type in {"MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE"}:
            return {"PASS_THROUGH"}
        if event.type in {"LEFTMOUSE", "RIGHTMOUSE"} and event.alt:
            return {"PASS_THROUGH"}
        if event.type.startswith("NUMPAD_"):
            return {"PASS_THROUGH"}

        if event.type == "ESC":
            self.running = False
            _remove_mode_overlay(self)
            # Convert markers -> wall now. After returning FINISHED, redo panel will appear.
            return self.execute(context)

        if event.type == "RIGHTMOUSE" and event.value == "PRESS":
            self._is_erasing = True
            cell = self._cell_from_mouse(context, event)
            if cell:
                self._erase_marker(context, cell)
            self._last_cell = cell
            return {"RUNNING_MODAL"}

        if event.type == "RIGHTMOUSE" and event.value == "RELEASE":
            self._is_erasing = False
            self._last_cell = None
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            self._is_dragging = True
            cell = self._cell_from_mouse(context, event)
            if cell:
                self._place_marker(context, cell)
            self._last_cell = cell
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            self._is_dragging = False
            self._last_cell = None
            return {"RUNNING_MODAL"}

        # Drag erase/place
        if event.type == "MOUSEMOVE":
            if self._is_erasing or self._is_dragging:
                cell = self._cell_from_mouse(context, event)
                if cell and cell != self._last_cell:
                    if self._is_erasing:
                        self._erase_marker(context, cell)
                    else:
                        self._place_marker(context, cell)
                    self._last_cell = cell
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}

        return {"RUNNING_MODAL"}


# ----------------------------
# Optimization Operator
# ----------------------------


class VIEW3D_OT_gridmap3d_optimize(Operator):
    bl_idname = "view3d.gridarch_optimize"
    bl_label = "Optimize (Boundary Cleanup)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not any(o.type == 'MESH' and is_tagged(o, TAG_KEY, TAG_VAL) for o in bpy.data.objects):
            self.report({"INFO"}, "No tagged meshes in scene")
            return {"CANCELLED"}

        # Optimize the active Plane-kind mesh (preferred) or fallback GridArch_Plane
        active = context.view_layer.objects.active
        plane_obj = None
        if active and active.type == 'MESH' and is_tagged(active, TAG_KEY, TAG_VAL) and active.get(PLANE_KIND_KEY) == PLANE_KIND_VAL:
            plane_obj = active
        else:
            plane_obj = bpy.data.objects.get(GRID_PLANE_NAME)

        if plane_obj and plane_obj.type == 'MESH' and plane_obj.data.polygons:
            make_mesh_data_undo_safe(plane_obj)
            rebuild_plane_as_max_rectangles(plane_obj, float(gridarch_settings(context).grid_size))
            optimize_keep_right_angles(plane_obj, angle_tol=1e-3)

        # If a matching wall exists for the selected plane, optimize it too.
        wall_obj = None
        if plane_obj:
            wname = plane_obj.name.replace("Plane", "AutoWall") if "Plane" in plane_obj.name else f"{plane_obj.name}_AutoWall"
            wall_obj = bpy.data.objects.get(wname)

        if wall_obj and wall_obj.type == 'MESH' and wall_obj.data.polygons:
            make_mesh_data_undo_safe(wall_obj)
            optimize_wall_mesh(wall_obj)
            optimize_keep_right_angles(wall_obj, angle_tol=1e-3)

        # Also optimize ALL Draw Wall objects in the working collection.
        # Walls are generated as a combined mesh of quads; boundary cleanup removes unnecessary boundary verts.
        for o in bpy.data.objects:
            if not (o and o.type == 'MESH' and is_tagged(o, TAG_KEY, TAG_VAL)):
                continue
            if o.get(WALL_KIND_KEY) != WALL_KIND_VAL:
                continue
            if not o.data.polygons:
                continue
            make_mesh_data_undo_safe(o)
            optimize_wall_mesh(o)
            optimize_keep_right_angles(o, angle_tol=1e-3)

        return {"FINISHED"}


# ----------------------------
# Fill Operator
# ----------------------------



def _flood_fill_interior(boundary_xy: set[tuple[int, int]]):
    """Given boundary cells on a 2D grid (x,y), return interior cells.

    Uses a standard outside flood fill on the bounding box + margin.
    """
    if not boundary_xy:
        return set()

    xs = [p[0] for p in boundary_xy]
    ys = [p[1] for p in boundary_xy]
    minx, maxx = min(xs) - 1, max(xs) + 1
    miny, maxy = min(ys) - 1, max(ys) + 1

    outside = set()
    stack = [(minx, miny)]

    def in_bounds(x, y):
        return minx <= x <= maxx and miny <= y <= maxy

    while stack:
        x, y = stack.pop()
        if (x, y) in outside:
            continue
        if (x, y) in boundary_xy:
            continue
        if not in_bounds(x, y):
            continue
        outside.add((x, y))
        stack.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])

    interior = set()
    for x in range(minx + 1, maxx):
        for y in range(miny + 1, maxy):
            if (x, y) in boundary_xy:
                continue
            if (x, y) in outside:
                continue
            interior.add((x, y))
    return interior


class VIEW3D_OT_gridmap3d_fill(Operator):
    bl_idname = "view3d.gridarch_fill"
    bl_label = "Fill Inside Loop"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = gridarch_settings(context)

        plane_obj = get_active_or_default_plane(context)
        if not plane_obj or not plane_obj.data.polygons:
            self.report({'INFO'}, "No PlaneMesh faces to fill")
            return {'CANCELLED'}

        # Read occupied plane cells from face centers (LOCAL space)
        bm = bmesh.new()
        bm.from_mesh(plane_obj.data)
        cells = set()
        z_counts = {}
        for f in bm.faces:
            c = _plane_face_center_local(f)
            x = int(floor(c.x / float(s.grid_size)))
            y = int(floor(c.y / float(s.grid_size)))
            z = int(round(c.z / float(Z_UNIT)))
            cells.add((x, y, z))
            z_counts[z] = z_counts.get(z, 0) + 1
        bm.free()

        if not cells:
            self.report({'INFO'}, "No placed plane cells")
            return {'CANCELLED'}

        # pick the most frequent Z layer
        z0 = max(z_counts.items(), key=lambda kv: kv[1])[0]
        boundary_xy = {(x, y) for (x, y, z) in cells if z == z0}
        interior_xy = _flood_fill_interior(boundary_xy)

        placed_count = 0
        for (x, y) in interior_xy:
            if (x, y) in boundary_xy:
                continue
            if (x, y, z0) in cells:
                continue
            add_grid_plane_tile(context, (x, y, z0), float(s.grid_size))
            placed_count += 1

        self.report({'INFO'}, f"Filled {placed_count} plane cells")
        return {'FINISHED'}


class VIEW3D_OT_gridarch_editcube(Operator):
    bl_idname = "view3d.gridarch_editcube"
    bl_label = "Edit Cube"
    bl_options = {"REGISTER", "UNDO"}

    land_on_bounds: BoolProperty(
        name="Land on BBox Top",
        default=False,
        description="Land new EditCube at the hit object's bounding-box top instead of the hit surface.",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "land_on_bounds")

    def execute(self, context):
        try:
            return bpy.ops.editcube.edit('INVOKE_DEFAULT', land_on_bounds=self.land_on_bounds)
        except Exception as ex:
            self.report({'ERROR'}, f"Failed to invoke Edit Cube: {ex}")
            return {'CANCELLED'}


# ----------------------------
# UI Panel
# ----------------------------


class VIEW3D_PT_gridmap3d(Panel):
    bl_label = "GridArch"
    bl_idname = "VIEW3D_PT_gridmap3d"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GridArch"

    def draw(self, context):
        layout = self.layout
        s = gridarch_settings(context)

        # --- Core settings
        col = layout.column(align=True)
        col.prop(s, "grid_size")
        col.separator()

        layout.separator()

        # --- Paint (integrated)
        col = layout.column(align=True)
        col.operator("view3d.gridarch_paint", text="Tile", icon="MESH_GRID")


        layout.separator()

        # --- Fill / Optimize
        col = layout.column(align=True)
        col.operator("view3d.gridarch_fill", text="Fill", icon="FACESEL")
        col.operator("view3d.gridarch_optimize", text="Auto Optimize", icon="MOD_DECIM")
        col.operator("view3d.gridarch_orthogonal_edges", text="Manual Optimize", icon="EDGESEL")

        layout.separator()

        # --- Layout
        box = layout.box()
        box.label(text="Layout")
        col = box.column(align=True)
        col.operator("ga.create_outer_wall", text="Auto Wall", icon="MOD_WIREFRAME")
        col.operator("view3d.gridarch_wall_mode", text="Draw Wall", icon="SNAP_EDGE")
        col.operator("editcube.edit", text="Edit Cube", icon="CUBE")
        col.operator("ga.create_opening_cut", text="Boolean", icon="MOD_BOOLEAN")

        # Optimize is unified into GridArch's Optimize button above.

        # --- BB Cube (inside GridArch)
        box = layout.box()
        box.label(text="Collision")

        col = box.column(align=True)
        col.operator("object.bb_create_cube", text="Create Collision")

        layout.separator()

        # --- Active plane related size (W x D x H in meters)
        sz = active_plane_related_size_m(context)
        box = layout.box()
        box.label(text="Active Plane Size (m)")
        if sz is None:
            box.label(text="(empty)")
        else:
            sx, sy, szm = sz
            box.label(text=f"{sx:.3f} × {sy:.3f} × {szm:.3f}")


# ----------------------------
# Register
# ----------------------------


classes = (
    GRIDARCH_Settings,

    VIEW3D_OT_gridmap3d_paint,
    VIEW3D_OT_gridarch_orthogonal_edges,
    VIEW3D_OT_gridmap3d_wall_mode,
    VIEW3D_OT_gridmap3d_optimize,
    VIEW3D_OT_gridmap3d_fill,
    ECB_OT_edit_cube,
    VIEW3D_OT_gridarch_editcube,

    # Integrated GridArch operators
    GA_OT_create_outer_wall,
    BB_OT_create_cube,
    GA_OT_create_opening_cut,

    VIEW3D_PT_gridmap3d,
)






def register():
    # Initialize handlers
    remove_wall_gn_handlers()
    failed = []
    for c in classes:
        try:
            bpy.utils.register_class(c)
        except Exception as ex:
            failed.append((c, ex))
            print(f"GridArch: failed to register {getattr(c, '__name__', str(c))}: {ex}")

    # Still set scene pointer if GRIDARCH_Settings registered
    try:
        bpy.types.Scene.gridarch = PointerProperty(type=GRIDARCH_Settings)
        # Backward-compatible alias for old code paths in the same Blender session.
        bpy.types.Scene.gridmap3d = bpy.types.Scene.gridarch
    except Exception as ex:
        print(f"GridArch: failed to set Scene properties: {ex}")
    # Backward-compatible alias for old code paths in the same Blender session.
    bpy.types.Scene.gridmap3d = bpy.types.Scene.gridarch
    try:
        active_context = bpy.context if getattr(bpy.context, "scene", None) else None
    except Exception:
        active_context = None
    if active_context:
        refresh_existing_wall_gn_modifiers(active_context)
    if ga_wall_gn_update_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(ga_wall_gn_update_handler)


def unregister():
    remove_wall_gn_handlers()

    # Scene pointer first
    if hasattr(bpy.types.Scene, "gridmap3d"):
        del bpy.types.Scene.gridmap3d
    if hasattr(bpy.types.Scene, "gridarch"):
        del bpy.types.Scene.gridarch
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception as ex:
            print(f"GridArch: failed to unregister {getattr(c, '__name__', str(c))}: {ex}")


if __name__ == "__main__":
    register()
