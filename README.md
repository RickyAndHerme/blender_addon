# GridArch Addon
グリッド単位でタイルをボンボン配置し、壁作成や壁の穴あけができる便利なアドオンです。

<img width="202" height="450" alt="{B075A09A-5F06-4741-863B-AF2D794017CC}" src="https://github.com/user-attachments/assets/12e89e95-a2a2-4cf7-90c1-8ac19a5a05f7" />
<img width="922" height="755" alt="{4762A472-7773-4AC6-9DB3-D337E0228ADF}" src="https://github.com/user-attachments/assets/4b62494e-9ed0-4dd0-a63c-fe54f70cae34" />

| ボタン名 | 機能 |
| -------- | -------- |
|Grid Size (m)|タイルの1辺の長さを調整|
|Tile|タイル配置モード。LMBで配置。RMBで削除。ESCキーでモードを抜ける(共通：各モードはESCキーでモードを終了する)|
|フィル|タイルで囲まれた内側をタイルで塗りつぶす|
|Auto Optimize|分割されたタイルと壁のエッジを最適化する|
|Manual Optimize|手動最適化モード。LMBでエッジの最適化。RMBで最適化を解除|
|Auto Wall|配置したタイルを選択した状態でAuto Wallすると外壁が作成される。新たにタイル設置後にAuto Wallしても壁を修正してくれる。|
|Draw Wall|壁設置モード。LMBでキューブを配置。RMBで削除。ESCキーでモードを抜けるとキューブが壁に変換される|
|Edit Cube|Ctrl+LMBでキューブを配置。Ctrl+RMBで削除。LMBで面をドラッグすると面を引き延ばす。RMBでオブジェクトを選択。モディファイアのソリッド化、ミラーを取り付けたただのPlane。左右対称に編集ができる|
|Boolean|壁と任意のオブジェクトを選択してBooleanすると、任意のオブジェクトの子に穴開け用のオブジェクトが作成され壁に穴を開ける。調整はモディファイアで可能|
|Create Collision|任意のオブジェクトを選択してCreate CollisionするとGodotEngineで使用できるコリジョンオブジェクトを子に作成する。プリミティブ形状やサイズも変更できる|
|Active Plane Size (m)|タイル、壁を含んだ全体のサイズをX,Y,Zでメートル表示。サイズを知りたいときに便利|

## 備考
- タイルは一つのオブジェクトで作られるので、2階建ての家などを作るときは編集モードでタイルを選択して分離するか、タイルオブジェクトごとコピーして編集するかしてください。
- 壁エッジの最適化はAuto Optimizeでしかできません。

まだまだ説明が不足しているがご勘弁願いたい。










