import bpy
import bmesh
from bpy_extras import view3d_utils
from mathutils import Vector

class OBJECT_OT_meta_sculpt_modal(bpy.types.Operator):
    """LMBでメタボールを配置して削る。ESC/RMBで終了"""
    bl_idname = "object.meta_sculpt_modal"
    bl_label = "Meta Sculpt Modal"
    bl_options = {'REGISTER', 'UNDO'}
    
    target_obj = None
    cutter_obj = None
    current_mball = None
    is_drawing = False
    last_pos = Vector((0, 0, 0))
    radius = 0.5
    
    def modal(self, context, event):
        # 画面の再描画を強制
        if context.area:
            context.area.tag_redraw()
        
        # 視点操作（カメラ回転、パン、ズーム）はBlender標準にパススルー
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} or event.type.startswith('NUMPAD'):
            return {'PASS_THROUGH'}
            
        # ESC または RMB でモード終了
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            self.cleanup(context)
            self.report({'INFO'}, "Sculpt Mode Finished.")
            return {'FINISHED'}
            
        # LMBのストローク制御
        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                self.start_stroke(context, event)
                return {'RUNNING_MODAL'}
            elif event.value == 'RELEASE':
                self.end_stroke(context)
                return {'RUNNING_MODAL'}
                
        # ドラッグ中の配置
        if event.type == 'MOUSEMOVE' and self.is_drawing:
            self.draw_stroke(context, event)
            return {'RUNNING_MODAL'}
            
        return {'RUNNING_MODAL'}

    def invoke(self, context, event):
        if not context.active_object or context.active_object.type != 'MESH':
            self.report({'WARNING'}, "Error: メッシュオブジェクトを選択してから実行してください。")
            return {'CANCELLED'}
            
        self.target_obj = context.active_object
        
        # 対象オブジェクトのサイズに合わせてメタボールの半径を動的に初期化
        dims = self.target_obj.dimensions
        self.radius = max(dims.x, dims.y, dims.z) * 0.05
        if self.radius < 0.01:
            self.radius = 0.1
            
        # Boolean用のカッターオブジェクトを検索または新規作成
        cutter_name = self.target_obj.name + "_MetaCutter"
        if cutter_name in bpy.data.objects:
            self.cutter_obj = bpy.data.objects[cutter_name]
        else:
            mesh = bpy.data.meshes.new(cutter_name)
            self.cutter_obj = bpy.data.objects.new(cutter_name, mesh)
            context.collection.objects.link(self.cutter_obj)
            
        # ★要望1：変換後のカッターオブジェクトをバウンド(BOUNDS)表示にする
        self.cutter_obj.display_type = 'BOUNDS'
        self.cutter_obj.hide_render = True # レンダリングから除外
            
        # ターゲットにBooleanモディファイアがなければ追加
        mod_name = "MetaSculpt_Difference"
        mod = self.target_obj.modifiers.get(mod_name)
        if not mod:
            mod = self.target_obj.modifiers.new(name=mod_name, type='BOOLEAN')
            mod.operation = 'DIFFERENCE'
            mod.solver = 'EXACT'
        
        # モディファイアの対象オブジェクトを毎回確実に指定する
        mod.object = self.cutter_obj
            
        context.window_manager.modal_handler_add(self)
        self.report({'INFO'}, "Sculpt Mode: LMBでドラッグして削る / ESCで終了")
        return {'RUNNING_MODAL'}
        
    def start_stroke(self, context, event):
        self.is_drawing = True
        self.last_pos = Vector((10000, 10000, 10000)) # 初期化
        
        # ストローク用のテンポラリ・メタボールを作成
        mball_data = bpy.data.metaballs.new("TempMeta")
        mball_data.resolution = self.radius * 0.2
        mball_data.render_resolution = self.radius * 0.2
        
        self.current_mball = bpy.data.objects.new("TempMetaObj", mball_data)
        
        # ★要望2：配置中のメタボールをワイヤーフレーム(WIRE)表示にする
        self.current_mball.display_type = 'WIRE'
        
        context.collection.objects.link(self.current_mball)
        
        self.add_mball_element(context, event)
        
    def draw_stroke(self, context, event):
        self.add_mball_element(context, event)
        
    def end_stroke(self, context):
        self.is_drawing = False
        
        if not self.current_mball or not self.current_mball.data.elements:
            if self.current_mball:
                bpy.data.objects.remove(self.current_mball, do_unlink=True)
            return

        # 変換前にビューレイヤーを強制更新し、メタボールのメッシュを確定させる
        context.view_layer.update()

        # メタボールをメッシュに変換（バッチ処理の要）
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = self.current_mball
        self.current_mball.select_set(True)
        bpy.ops.object.convert(target='MESH')
        
        converted_mesh = context.active_object
        
        # ★要望3の再修正：Boolean(UNION)演算による結合
        # 内部に余分な面を残さないための正攻法。ただし計算コストと破綻リスクは伴う。
        if not self.cutter_obj.data.vertices:
            # 初回はデータをコピーするだけ
            self.cutter_obj.data = converted_mesh.data.copy()
        else:
            mod = converted_mesh.modifiers.new(name="TempUnion", type='BOOLEAN')
            mod.operation = 'UNION'
            mod.object = self.cutter_obj
            mod.solver = 'EXACT' # 精度重視で内部交差を処理
            
            context.view_layer.update()
            
            context.view_layer.objects.active = converted_mesh
            bpy.ops.object.modifier_apply(modifier="TempUnion")
            
            # 結果をカッターオブジェクトのデータとして上書き
            old_data = self.cutter_obj.data
            self.cutter_obj.data = converted_mesh.data.copy()
            bpy.data.meshes.remove(old_data)
            
        # 一時メッシュをクリーンアップ
        bpy.data.objects.remove(converted_mesh, do_unlink=True)
        self.current_mball = None
        
        # ターゲットオブジェクトを再度アクティブに戻す
        context.view_layer.objects.active = self.target_obj
        self.target_obj.select_set(True)
            
    def add_mball_element(self, context, event):
        # 2Dマウス座標を3Dベクトルに変換
        region = context.region
        rv3d = context.region_data
        coord = (event.mouse_region_x, event.mouse_region_y)
        
        view_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        
        depsgraph = context.evaluated_depsgraph_get()
        target_eval = self.target_obj.evaluated_get(depsgraph)
        
        # Raycastの計算 (対象オブジェクトのローカル座標系に変換して照射)
        mat_inv = self.target_obj.matrix_world.inverted()
        ray_origin_obj = mat_inv @ ray_origin
        ray_dir_obj = (mat_inv.to_3x3() @ view_vector).normalized()
        
        # Raycast実行
        result, loc, normal, index = target_eval.ray_cast(ray_origin_obj, ray_dir_obj)
        
        if result:
            world_loc = self.target_obj.matrix_world @ loc
            
            # 直前の配置位置からの距離を測定し、近すぎる場合はスキップ（処理の軽量化）
            if (world_loc - self.last_pos).length < self.radius * 0.4:
                return
                
            self.last_pos = world_loc
            
            # メタボール要素の追加
            ele = self.current_mball.data.elements.new()
            ele.co = self.current_mball.matrix_world.inverted() @ world_loc
            ele.radius = self.radius
            ele.type = 'BALL'

    def cleanup(self, context):
        if self.current_mball:
            bpy.data.objects.remove(self.current_mball, do_unlink=True)
        if self.target_obj:
            self.target_obj.select_set(True)
            context.view_layer.objects.active = self.target_obj


# UIパネルの定義
class VIEW3D_PT_meta_sculpt(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Meta Sculpt'
    bl_label = "Sculpt Tool"

    def draw(self, context):
        layout = self.layout
        layout.operator("object.meta_sculpt_modal", text="Start Sculpt", icon='SCULPTMODE_HLT')


classes = (
    OBJECT_OT_meta_sculpt_modal,
    VIEW3D_PT_meta_sculpt,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()