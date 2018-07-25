import base64
import collections
import itertools
import math
import struct
import pprint # pylint: disable=unused-import

from panda3d.core import * # pylint: disable=wildcard-import
try:
    from panda3d import bullet
    HAVE_BULLET = True
except ImportError:
    HAVE_BULLET = False


class Converter():
    _COMPONENT_TYPE_MAP = {
        5120: GeomEnums.NT_int8,
        5121: GeomEnums.NT_uint8,
        5122: GeomEnums.NT_int16,
        5123: GeomEnums.NT_uint16,
        5124: GeomEnums.NT_int32,
        5125: GeomEnums.NT_uint32,
        5126: GeomEnums.NT_float32,
    }
    _COMPONENT_NUM_MAP = {
        'MAT4': 16,
        'VEC4': 4,
        'VEC3': 3,
        'VEC2': 2,
        'SCALAR': 1,
    }
    _ATTRIB_CONENT_MAP = {
        'position': GeomEnums.C_point,
        'normal': GeomEnums.C_normal,
        'texcoord': GeomEnums.C_texcoord,
        'color': GeomEnums.C_color,
    }
    _ATTRIB_NAME_MAP = {
        'position': 'vertex',
        'weight': 'transform_weight',
        'joint': 'transform_index',
    }
    _PRIMITIVE_MODE_MAP = {
        0: GeomPoints,
        1: GeomLines,
        3: GeomLinestrips,
        4: GeomTriangles,
        5: GeomTristrips,
        6: GeomTrifans,
    }

    def __init__(self):
        self.cameras = {}
        self.buffers = {}
        self.lights = {}
        self.textures = {}
        self.mat_states = {}
        self.mat_mesh_map = {}
        self.meshes = {}
        self.nodes = {}
        self.node_paths = {}
        self.scenes = {}
        self.characters = {}
        self.joint_map = {}

        self._joint_nodes = set()

        # Scene props
        self.active_scene = NodePath(ModelRoot('default'))
        self.background_color = (0, 0, 0)
        self.active_camera = None

    def update(self, gltf_data, writing_bam=False):
        #pprint.pprint(gltf_data)

        # Convert data
        for buffid, gltf_buffer in enumerate(gltf_data.get('buffers', [])):
            self.load_buffer(buffid, gltf_buffer)

        for camid, gltf_cam in enumerate(gltf_data.get('cameras', [])):
            self.load_camera(camid, gltf_cam)

        if 'extensions' in gltf_data and 'KHR_lights' in gltf_data['extensions']:
            lights = gltf_data['extensions']['KHR_lights'].get('lights', [])
            for lightid, gltf_light in enumerate(lights):
                self.load_light(lightid, gltf_light)

        for texid, gltf_tex in enumerate(gltf_data.get('textures', [])):
            self.load_texture(texid, gltf_tex, gltf_data)
        self.load_fallback_texture()

        for matid, gltf_mat in enumerate(gltf_data.get('materials', [])):
            self.load_material(matid, gltf_mat)

        for skinid, gltf_skin in enumerate(gltf_data.get('skins', [])):
            self.load_skin(skinid, gltf_skin, gltf_data)

        for meshid, gltf_mesh in enumerate(gltf_data.get('meshes', [])):
            self.load_mesh_new(meshid, gltf_mesh, gltf_data)

        for nodeid, gltf_node in enumerate(gltf_data.get('nodes', [])):
            node_name = gltf_node.get('name', 'node'+str(nodeid))
            node = self.nodes.get(nodeid, PandaNode(node_name))
            self.nodes[nodeid] = node

        # If we support writing bam 6.40, we can safely write out
        # instanced lights.  If not, we have to copy it.
        copy_lights = writing_bam and not hasattr(BamWriter, 'root_node')

        # Build scenegraphs
        def add_node(root, gltf_scene, nodeid):
            try:
                gltf_node = gltf_data['nodes'][nodeid]
            except IndexError:
                print("Could not find node with index: {}".format(nodeid))
                return

            node_name = gltf_node.get('name', 'node'+str(nodeid))
            if nodeid in self._joint_nodes:
                # don't handle joints here
                return
            panda_node = self.nodes[nodeid]

            if 'extras' in gltf_scene and 'hidden_nodes' in gltf_scene['extras']:
                if nodeid in gltf_scene['extras']['hidden_nodes']:
                    panda_node = panda_node.make_copy()

            np = self.node_paths.get(nodeid, root.attach_new_node(panda_node))
            self.node_paths[nodeid] = np

            if 'mesh' in gltf_node:
                mesh = self.meshes[gltf_node['mesh']]
                np.attach_new_node(mesh)
                char = self.characters[gltf_node['skin']]
                np.attach_new_node(char)

                self.combine_mesh_skin(mesh, char, gltf_node['skin'])
            if 'skin' in gltf_node and not 'mesh' in gltf_node:
                print(
                    "Warning: node {} has a skin but no mesh"
                    .format(primitiveid)
                )
            if 'camera' in gltf_node:
                camid = gltf_node['camera']
                cam = self.cameras[camid]
                np.attach_new_node(cam)
            if 'extensions' in gltf_node:
                if 'KHR_materials_common' in gltf_node['extensions']:
                    lightid = gltf_node['extensions']['KHR_materials_common']['light']
                    light = self.lights[lightid]
                    if copy_lights:
                        light = light.make_copy()
                    lnp = np.attach_new_node(light)
                    if isinstance(light, Light):
                        root.set_light(lnp)

                if HAVE_BULLET and 'BLENDER_physics' in gltf_node['extensions']:
                    phy = gltf_node['extensions']['BLENDER_physics']
                    shape = None
                    collision_shape = phy['collisionShapes'][0]
                    bounding_box = collision_shape['boundingBox']
                    radius = max(bounding_box[0], bounding_box[1]) / 2.0
                    height = bounding_box[2]
                    geomnode = None
                    static = 'static' in phy and phy['static']
                    if 'mesh' in collision_shape:
                        try:
                            geomnode = self.meshes[collision_shape['mesh']]
                        except KeyError:
                            print(
                                "Could not find physics mesh ({}) for object ({})"
                                .format(collision_shape['mesh'], nodeid)
                            )

                    shape_type = collision_shape['shapeType']
                    if shape_type == 'BOX':
                        shape = bullet.BulletBoxShape(LVector3(*bounding_box) / 2.0)
                    elif shape_type == 'SPHERE':
                        shape = bullet.BulletSphereShape(max(bounding_box) / 2.0)
                    elif shape_type == 'CAPSULE':
                        shape = bullet.BulletCapsuleShape(radius, height - 2.0 * radius, bullet.ZUp)
                    elif shape_type == 'CYLINDER':
                        shape = bullet.BulletCylinderShape(radius, height, bullet.ZUp)
                    elif shape_type == 'CONE':
                        shape = bullet.BulletConeShape(radius, height, bullet.ZUp)
                    elif shape_type == 'CONVEX_HULL':
                        if geomnode:
                            shape = bullet.BulletConvexHullShape()

                            for geom in geomnode.get_geoms():
                                shape.add_geom(geom)
                    elif shape_type == 'MESH':
                        if geomnode:
                            mesh = bullet.BulletTriangleMesh()
                            for geom in geomnode.get_geoms():
                                mesh.add_geom(geom)
                            shape = bullet.BulletTriangleMeshShape(mesh, dynamic=not static)
                    else:
                        print("Unknown collision shape ({}) for object ({})".format(shape_type, nodeid))

                    if shape is not None:
                        phynode = bullet.BulletRigidBodyNode(node_name)
                        phynode.add_shape(shape)
                        np.attach_new_node(phynode)
                        if not static:
                            phynode.set_mass(phy['mass'])
                    else:
                        print("Could not create collision shape for object ({})".format(nodeid))
                elif not HAVE_BULLET:
                    print("Bullet is unavailable, not converting collision shape for object ({})".format(nodeid))
            if 'extras' in gltf_node:
                for key, value in gltf_node['extras'].items():
                    np.set_tag(key, str(value))


            for child_nodeid in gltf_node.get('children', []):
                add_node(np, gltf_scene, child_nodeid)

            # Handle visibility after children are loaded
            def visible_recursive(node, visible):
                if visible:
                    node.show()
                else:
                    node.hide()
                for child in node.get_children():
                    visible_recursive(child, visible)
            if 'extras' in gltf_scene and 'hidden_nodes' in gltf_scene['extras']:
                if nodeid in gltf_scene['extras']['hidden_nodes']:
                    #print('Hiding', np)
                    visible_recursive(np, False)
                else:
                    #print('Showing', np)
                    visible_recursive(np, True)

            # Check if we need to deal with negative scale values
            scale = panda_node.get_transform().get_scale()
            negscale = scale.x * scale.y * scale.z < 0
            if negscale:
                for geomnode in np.find_all_matches('**/+GeomNode'):
                    tmp = geomnode.get_parent().attach_new_node(PandaNode('ReverseCulling'))
                    tmp.set_attrib(CullFaceAttrib.make_reverse())
                    geomnode.reparent_to(tmp)

        for sceneid, gltf_scene in enumerate(gltf_data.get('scenes', [])):
            scene_name = gltf_scene.get('name', 'scene'+str(sceneid))
            scene_root = NodePath(ModelRoot(scene_name))

            node_list = gltf_scene['nodes']
            if 'extras' in gltf_scene and 'hidden_nodes' in gltf_scene['extras']:
                node_list += gltf_scene['extras']['hidden_nodes']

            for nodeid in node_list:
                add_node(scene_root, gltf_scene, nodeid)

            self.scenes[sceneid] = scene_root

        # Update node transforms for glTF nodes that have a NodePath
        for nodeid, gltf_node in enumerate(gltf_data.get('nodes', [])):
            if nodeid not in self.node_paths:
                continue
            np = self.node_paths[nodeid]
            np.set_pos(*gltf_node.get('translation', [0, 0, 0]))
            np.set_hpr(self.load_quaternion_as_hpr(gltf_node.get('rotation', [0, 0, 0, 1])))
            np.set_scale(*gltf_node.get('scale', [1, 1, 1]))


        # Set the active scene
        sceneid = gltf_data.get('scene', None)
        if sceneid in self.scenes:
            self.active_scene = self.scenes[sceneid]
        if 'scenes' in gltf_data:
            gltf_scene = gltf_data['scenes'][sceneid]
            if 'extras' in gltf_scene:
                if 'background_color' in gltf_scene['extras']:
                    self.background_color = gltf_scene['extras']['background_color']
                if 'active_camera' in gltf_scene['extras']:
                    self.active_camera = gltf_scene['extras']['active_camera']

    def load_matrix(self, mat):
        lmat = LMatrix4()

        for i in range(4):
            lmat.set_row(i, LVecBase4(*mat[i * 4: i * 4 + 4]))
        return lmat

    def decompose_matrix(self, mat):
        mat = LMatrix4(mat)
        translation = mat.get_row3(3)
        mat.set_row(3, LVector3(0, 0, 0))
        scale = [mat.get_row3(i).length() for i in range(3)]
        for i in range(3):
            mat.set_row(i, mat.get_row3(i) / scale[i])
        rot_quat = LQuaternion()
        rot_quat.set_from_matrix(mat.getUpper3())
        rotation = rot_quat.get_hpr()

        return translation, rotation, LVector3(*scale)

    def load_quaternion_as_hpr(self, quaternion):
        quat = LQuaternion(quaternion[3], quaternion[0], quaternion[1], quaternion[2])
        return quat.get_hpr()

    def load_buffer(self, buffid, gltf_buffer):
        uri = gltf_buffer['uri']
        if uri.startswith('data:application/octet-stream;base64'):
            buff_data = gltf_buffer['uri'].split(',')[1]
            buff_data = base64.b64decode(buff_data)
        else:
            print(
                "Buffer {} has an unsupported uri ({}), using a zero filled buffer instead"
                .format(buffid, uri)
            )
            buff_data = bytearray(gltf_buffer['byteLength'])
        self.buffers[buffid] = buff_data

    def make_texture_srgb(self, texture):
        if texture.get_num_components() == 3:
            texture.set_format(Texture.F_srgb)
        elif texture.get_num_components() == 4:
            texture.set_format(Texture.F_srgb_alpha)

    def load_fallback_texture(self):
        texture = Texture('pbr-fallback')
        texture.setup_2d_texture(1, 1, Texture.T_unsigned_byte, Texture.F_rgba)
        texture.set_clear_color(LColor(1, 1, 1, 1))

        self.textures['__bp-pbr-fallback'] = texture

    def load_texture(self, texid, gltf_tex, gltf_data):
        if 'source' not in gltf_tex:
            print("Texture '{}' has no source, skipping".format(texid))
            return

        source = gltf_data['images'][gltf_tex['source']]
        uri = Filename.fromOsSpecific(source['uri'])
        texture = TexturePool.load_texture(uri, 0, False, LoaderOptions())
        use_srgb = False
        if 'format' in gltf_tex and gltf_tex['format'] in (0x8C40, 0x8C42):
            use_srgb = True
        elif 'internalFormat' in gltf_tex and gltf_tex['internalFormat'] in (0x8C40, 0x8C42):
            use_srgb = True

        if use_srgb:
            self.make_texture_srgb(texture)
        self.textures[texid] = texture

    def load_material(self, matid, gltf_mat):
        matname = gltf_mat.get('name', 'mat'+str(matid))
        state = self.mat_states.get(matid, RenderState.make_empty())

        if matid not in self.mat_mesh_map:
            self.mat_mesh_map[matid] = []

        pmat = Material(matname)
        pbr_fallback = {'index': '__bp-pbr-fallback', 'texcoord': 0}
        textures = []

        if 'pbrMetallicRoughness' in gltf_mat:
            pbrsettings = gltf_mat['pbrMetallicRoughness']

            pmat.set_base_color(LColor(*pbrsettings.get('baseColorFactor', [1.0, 1.0, 1.0, 1.0])))
            textures.append(pbrsettings.get('baseColorTexture', pbr_fallback)['index'])
            if textures[-1] in self.textures:
                self.make_texture_srgb(self.textures[textures[-1]])

            pmat.set_metallic(pbrsettings.get('metallicFactor', 1.0))
            textures.append(pbrsettings.get('metallicTexture', pbr_fallback)['index'])

            pmat.set_roughness(pbrsettings.get('roughnessFactor', 1.0))
            textures.append(pbrsettings.get('roughnessTexture', pbr_fallback)['index'])

        if 'extensions' in gltf_mat and 'BP_materials_legacy' in gltf_mat['extensions']:
            matsettings = gltf_mat['extensions']['BP_materials_legacy']['bpLegacy']
            pmat.set_shininess(matsettings['shininessFactor'])
            pmat.set_ambient(LColor(*matsettings['ambientFactor']))

            if 'diffuseTexture' in matsettings:
                texture = matsettings['diffuseTexture']
                textures.append(texture)
                if matsettings['diffuseTextureSrgb'] and texture in self.textures:
                    self.make_texture_srgb(self.textures[texture])
            else:
                pmat.set_diffuse(LColor(*matsettings['diffuseFactor']))

            if 'emissionTexture' in matsettings:
                texture = matsettings['emissionTexture']
                textures.append(texture)
                if matsettings['emissionTextureSrgb'] and texture in self.textures:
                    self.make_texture_srgb(self.textures[texture])
            else:
                pmat.set_emission(LColor(*matsettings['emissionFactor']))

            if 'specularTexture' in matsettings:
                texture = matsettings['specularTexture']
                textures.append(texture)
                if matsettings['specularTextureSrgb'] and texture in self.textures:
                    self.make_texture_srgb(self.textures[texture])
            else:
                pmat.set_specular(LColor(*matsettings['specularFactor']))
        pmat.set_twoside(gltf_mat.get('doubleSided', False))


        state = state.set_attrib(MaterialAttrib.make(pmat))

        for i, tex in enumerate(textures):
            texdata = self.textures.get(tex, None)
            if texdata is None:
                print("Could not find texture for key: {}".format(tex))
                continue

            tex_attrib = TextureAttrib.make()
            texstage = TextureStage(str(i))
            texstage.set_texcoord_name(InternalName.get_texcoord_name('0'))

            if texdata.get_num_components() == 4:
                state = state.set_attrib(TransparencyAttrib.make(TransparencyAttrib.M_alpha))

            tex_attrib = tex_attrib.add_on_stage(texstage, texdata)
            state = state.set_attrib(tex_attrib)

        # Remove stale meshes
        self.mat_mesh_map[matid] = [
            pair for pair in self.mat_mesh_map[matid] if pair[0] in self.meshes
        ]

        # Reload the material
        for meshid, geom_idx in self.mat_mesh_map[matid]:
            self.meshes[meshid].set_geom_state(geom_idx, state)

        self.mat_states[matid] = state

    def create_anim(self, character, root_bone_id, animid, gltf_anim, gltf_data):
        anim_name = gltf_anim.get('name', 'anim'+str(animid))
        samplers = gltf_anim['samplers']

        # Blender exports the same number of elements in each time parameter, so find
        # one and assume that the number of elements is the number of frames
        time_acc_id = samplers[0]['input']
        time_acc = gltf_data['accessors'][time_acc_id]
        fps = 1 / time_acc['min'][0]
        num_frames = time_acc['count']

        bundle_name = anim_name
        bundle = AnimBundle(bundle_name, fps, num_frames)
        skeleton = AnimGroup(bundle, '<skeleton>')

        def create_anim_channel(parent, boneid):
            bone = gltf_data['nodes'][boneid]
            bone_name = bone.get('name', 'bone'+str(boneid))
            channels = [chan for chan in gltf_anim['channels'] if chan['target']['node'] == boneid]
            joint_mat = character.find_joint(bone_name).get_transform()

            group = AnimChannelMatrixXfmTable(parent, bone_name)

            def get_accessor(path):
                accessors = [
                    gltf_data['accessors'][samplers[chan['sampler']]['output']]
                    for chan in channels
                    if chan['target']['path'] == path
                ]

                return accessors[0] if accessors else None

            def extract_chan_data(path):
                vals = []
                acc = get_accessor(path)

                buff_view = gltf_data['bufferViews'][acc['bufferView']]
                buff_data = self.buffers[buff_view['buffer']]
                start = acc['byteOffset'] + buff_view['byteOffset']
                end = buff_view['byteOffset'] + buff_view['byteLength']

                if path == 'rotation':
                    data = [struct.unpack_from('<ffff', buff_data, idx) for idx in range(start, end, 4 * 4)]
                    vals = [
                        [i[0] for i in data],
                        [i[1] for i in data],
                        [i[2] for i in data],
                        [i[3] for i in data]
                    ]
                    #convert quats to hpr
                    vals = list(zip(*[LQuaternion(i[3], i[0], i[1], i[2]).get_hpr() for i in zip(*vals)]))
                else:
                    data = [struct.unpack_from('<fff', buff_data, idx) for idx in range(start, end, 3 * 4)]
                    vals = [
                        [i[0] for i in data],
                        [i[1] for i in data],
                        [i[2] for i in data]
                    ]

                return vals

            # Create default animaton data
            translation, rotation, scale = self.decompose_matrix(joint_mat)
            loc_vals = list(zip(
                *[(translation.get_x(), translation.get_y(), translation.get_z()) for i in range(num_frames)]
            ))
            rot_vals = list(zip(
                *[(rotation.get_x(), rotation.get_y(), rotation.get_z()) for i in range(num_frames)]
            ))
            scale_vals = list(zip(
                *[(scale.get_x(), scale.get_y(), scale.get_z()) for i in range(num_frames)]
            ))

            # Override defaults with any found animation data
            if get_accessor('translation') is not None:
                loc_vals = extract_chan_data('translation')
            if get_accessor('rotation') is not None:
                rot_vals = extract_chan_data('rotation')
            if get_accessor('scale') is not None:
                scale_vals = extract_chan_data('scale')

            # Write data to tables
            group.set_table(b'x', CPTAFloat(PTAFloat(loc_vals[0])))
            group.set_table(b'y', CPTAFloat(PTAFloat(loc_vals[1])))
            group.set_table(b'z', CPTAFloat(PTAFloat(loc_vals[2])))

            group.set_table(b'h', CPTAFloat(PTAFloat(rot_vals[0])))
            group.set_table(b'p', CPTAFloat(PTAFloat(rot_vals[1])))
            group.set_table(b'r', CPTAFloat(PTAFloat(rot_vals[2])))

            group.set_table(b'i', CPTAFloat(PTAFloat(scale_vals[0])))
            group.set_table(b'j', CPTAFloat(PTAFloat(scale_vals[1])))
            group.set_table(b'k', CPTAFloat(PTAFloat(scale_vals[2])))

            for childid in bone.get('children', []):
                create_anim_channel(group, childid)

        create_anim_channel(skeleton, root_bone_id)
        character.add_child(AnimBundleNode(character.name, bundle))

    def load_skin(self, skinid, gltf_skin, gltf_data):
        skinname = gltf_skin.get('name', 'char'+str(skinid))
        #print("Creating character for", skinname)
        root = gltf_data['nodes'][gltf_skin['skeleton']]

        character = Character(skinname)
        bundle = character.get_bundle(0)
        skeleton = PartGroup(bundle, "<skeleton>")
        jvtmap = {}

        bind_mats = []
        ibmacc = gltf_data['accessors'][gltf_skin['inverseBindMatrices']]
        ibmbv = gltf_data['bufferViews'][ibmacc['bufferView']]
        ibmdata = self.buffers[ibmbv['buffer']]

        joint_ids = set()

        for i in range(ibmacc['count']):
            mat = struct.unpack_from('<{}'.format('f'*16), ibmdata, i * 16 * 4)
            #print('loaded', mat)
            mat = self.load_matrix(mat)
            mat.invert_in_place()
            bind_mats.append(mat)

        def create_joint(parent, nodeid, node, transform):
            node_name = node.get('name', 'bone'+str(nodeid))
            inv_transform = LMatrix4(transform)
            inv_transform.invert_in_place()
            joint_index = None
            joint_mat = LMatrix4.ident_mat()
            if nodeid in gltf_skin['joints']:
                joint_index = gltf_skin['joints'].index(nodeid)
                joint_mat = bind_mats[joint_index]
                self._joint_nodes.add(nodeid)

            # glTF uses an absolute bind pose, Panda wants it local
            bind_pose = joint_mat * inv_transform
            joint = CharacterJoint(character, bundle, parent, node_name, bind_pose)

            # Non-deforming bones are not in the skin's jointNames, don't add them to the jvtmap
            if joint_index is not None:
                jvtmap[joint_index] = JointVertexTransform(joint)

            joint_ids.add(nodeid)

            for child in node.get('children', []):
                #print("Create joint for child", child)
                bone_node = gltf_data['nodes'][child]
                create_joint(joint, child, bone_node, bind_pose * transform)

        create_joint(skeleton, gltf_skin['skeleton'], root, LMatrix4.ident_mat())

        self.characters[skinid] = character
        self.joint_map[skinid] = jvtmap

        # convert animations
        #print("Looking for actions for", skinname, joint_ids)
        anims = [
            (animid, anim)
            for animid, anim in enumerate(gltf_data.get('animations', []))
            if joint_ids & {chan['target']['node'] for chan in anim['channels']}
        ]

        if anims:
            #print("Found anims for", skinname)
            for animid, gltf_anim in anims:
                #print("\t", gltf_anim.get('name', 'anim'+str(animid)))
                self.create_anim(character, gltf_skin['skeleton'], animid, gltf_anim, gltf_data)

    def load_primitive(self, geom_node, gltf_primitive, gltf_data):
        # Build Vertex Format
        vformat = GeomVertexFormat()
        mesh_attribs = gltf_primitive['attributes']
        accessors = [
            {**gltf_data['accessors'][acc_idx], 'attrib': attrib_name}
            for attrib_name, acc_idx in mesh_attribs.items()
        ]
        accessors = sorted(accessors, key=lambda x: x['bufferView'])
        data_copies = []
        is_skinned = 'JOINTS_0' in mesh_attribs

        for buffview, accs in itertools.groupby(accessors, key=lambda x: x['bufferView']):
            buffview = gltf_data['bufferViews'][buffview]
            accs = sorted(accs, key=lambda x: x['byteOffset'])
            is_interleaved = len(accs) > 1 and accs[1]['byteOffset'] < buffview['byteStride']

            varray = GeomVertexArrayFormat()
            for acc in accs:
                # Gather column information
                attrib_parts = acc['attrib'].lower().split('_')
                attrib_name = self._ATTRIB_NAME_MAP.get(attrib_parts[0], attrib_parts[0])
                if attrib_name == 'texcoord' and len(attrib_parts) > 1:
                    print(attrib_parts)
                    internal_name = InternalName.make(attrib_name, int(attrib_parts[1]))
                else:
                    internal_name = InternalName.make(attrib_name)
                num_components = self._COMPONENT_NUM_MAP[acc['type']]
                numeric_type = self._COMPONENT_TYPE_MAP[acc['componentType']]
                content = self._ATTRIB_CONENT_MAP.get(attrib_name, GeomEnums.C_other)

                # Add this accessor as a column to the current vertex array format
                varray.add_column(internal_name, num_components, numeric_type, content)

                if not is_interleaved:
                    # Start a new vertex array format
                    vformat.add_array(varray)
                    varray = GeomVertexArrayFormat()
                    data_copies.append((
                        buffview['buffer'],
                        acc['byteOffset'] + buffview['byteOffset'],
                        acc['count'],
                        buffview.get('byteStride', 1)
                    ))

            if is_interleaved:
                vformat.add_array(varray)
                data_copies.append((
                    buffview['buffer'],
                    buffview['byteOffset'],
                    accs[0]['count'],
                    buffview.get('byteStride', 1)
                ))

        if is_skinned:
            aspec = GeomVertexAnimationSpec()
            aspec.set_hardware(max(gltf_data['accessors'][mesh_attribs['JOINTS_0']]['max']) + 1, False)
            vformat.set_animation(aspec)

        # Copy data from buffers
        reg_format = GeomVertexFormat.register_format(vformat)
        vdata = GeomVertexData(geom_node.name, reg_format, GeomEnums.UH_stream)

        for array_idx, data_info in enumerate(data_copies):
            handle = vdata.modify_array(array_idx).modify_handle()
            handle.unclean_set_num_rows(data_info[2])

            buff = self.buffers[data_info[0]]
            start = data_info[1]
            end = start + data_info[2] * data_info[3]
            handle.copy_data_from(buff[start:end])
            handle = None

        # Construct primitive
        primitiveid = geom_node.get_num_geoms()
        try:
            prim = self._PRIMITIVE_MODE_MAP[gltf_primitive['mode']](GeomEnums.UH_static)
        except KeyError:
            print(
                "Warning: primitive {} on mesh {} has an unsupported mode"
                .format(primitiveid, geom_node.name)
            )
            prim = GeomPoints(GeomEnums.UH_static)

        if 'indices' in gltf_primitive:
            index_acc = gltf_data['accessors'][gltf_primitive['indices']]
            prim.set_index_type(self._COMPONENT_TYPE_MAP[index_acc['componentType']])

            handle = prim.modify_vertices(index_acc['count']).modify_handle()
            handle.unclean_set_num_rows(index_acc['count'])

            buffview = gltf_data['bufferViews'][index_acc['bufferView']]
            buff = self.buffers[buffview['buffer']]
            start = buffview['byteOffset']
            end = start + index_acc['count'] * buffview.get('byteStride', 1) * prim.index_stride
            handle.copy_data_from(buff[start:end])
            handle = None

        # Assign a material
        matid = gltf_primitive.get('material', None)
        if matid is None:
            print(
                "Warning: mesh {} has a primitive with no material, using an empty RenderState"
                .format(geom_node.name)
            )
            mat = RenderState.make_empty()
        elif matid not in self.mat_states:
            print(
                "Warning: material with name {} has no associated mat state, using an empty RenderState"
                .format(matid)
            )
            mat = RenderState.make_empty()
        else:
            mat = self.mat_states[gltf_primitive['material']]
            self.mat_mesh_map[gltf_primitive['material']].append((geom_node.name, primitiveid))

        # Add this primitive back to the geom node
        #ss = StringStream()
        #vdata.write(ss)
        ###prim.write(ss, 2)
        #print(ss.data.decode('utf8'))
        geom = Geom(vdata)
        geom.add_primitive(prim)
        geom_node.add_geom(geom, mat)

    def load_mesh_new(self, meshid, gltf_mesh, gltf_data):
        mesh_name = gltf_mesh.get('name', 'mesh'+str(meshid))
        node = self.meshes.get(meshid, GeomNode(mesh_name))

        # Clear any existing mesh data
        node.remove_all_geoms()

        # Load primitives
        for gltf_primitive in gltf_mesh['primitives']:
            self.load_primitive(node, gltf_primitive, gltf_data)

        # Save mesh
        self.meshes[meshid] = node

    def combine_mesh_skin(self, geom_node, character, skinid):
        jvtmap = collections.OrderedDict(sorted(self.joint_map[skinid].items()))
        xformtable = TransformTable()
        xfblendtable = TransformBlendTable()

        for joint in jvtmap.values():
            xformtable.add_transform(joint)

        for geom in geom_node.modify_geoms():
            gvd = geom.modify_vertex_data()
            #gvd.set_transform_blend_table(xfblendtable)
            #gvd.set_transform_table(xformtable)

    def load_mesh(self, meshid, gltf_mesh, gltf_data):
        node = self.meshes.get(meshid, GeomNode(gltf_mesh['name']))

        # Clear any existing mesh data
        node.remove_all_geoms()

        # Check for skinning data
        mesh_attribs = gltf_mesh['primitives'][0]['attributes']
        is_skinned = 'WEIGHTS_0' in mesh_attribs

        # Describe the vertex data
        vert_array = GeomVertexArrayFormat()
        vert_array.add_column(InternalName.get_vertex(), 3, GeomEnums.NT_float32, GeomEnums.C_point)
        vert_array.add_column(InternalName.get_normal(), 3, GeomEnums.NT_float32, GeomEnums.C_normal)

        if is_skinned:
            # Find all nodes that use this mesh and try to find a skin
            _, gltf_node = [
                (i, gltf_node)
                for i, gltf_node in enumerate(gltf_data['nodes'])
                if 'mesh' in gltf_node and meshid == gltf_node['mesh'] and 'skin' in gltf_node
            ][0]
            #gltf_node = [gltf_node for gltf_node in gltf_nodes if 'skin' in gltf_node][0]
            gltf_skin = gltf_data['skins'][gltf_node['skin']]

            jvtmap = self.create_character(gltf_node, gltf_skin, gltf_data)
            tb_va = GeomVertexArrayFormat()
            tb_va.add_column(InternalName.get_transform_blend(), 1, GeomEnums.NTUint16, GeomEnums.CIndex)
            tbtable = TransformBlendTable()

        uv_layers = [
            i.replace('TEXCOORD_', '')
            for i in gltf_mesh['primitives'][0]['attributes']
            if i.startswith('TEXCOORD_')
        ]
        for uv_layer in uv_layers:
            vert_array.add_column(InternalName.get_texcoord_name(uv_layer), 2, GeomEnums.NTFloat32, GeomEnums.CTexcoord)

        col_layers = [
            i.replace('COLOR_', '')
            for i in gltf_mesh['primitives'][0]['attributes']
            if i.startswith('COLOR_')
        ]
        for col_layer in col_layers:
            vert_array.add_column(InternalName.get_color().append(col_layer), 3, GeomEnums.NTFloat32, GeomEnums.CColor)

        #reg_format = GeomVertexFormat.register_format(GeomVertexFormat(vert_array))
        vformat = GeomVertexFormat()
        vformat.add_array(vert_array)
        if is_skinned:
            vformat.add_array(tb_va)
            aspec = GeomVertexAnimationSpec()
            aspec.set_panda()
            vformat.set_animation(aspec)
        reg_format = GeomVertexFormat.register_format(vformat)
        vdata = GeomVertexData(gltf_mesh['name'], reg_format, GeomEnums.UH_stream)
        if is_skinned:
            vdata.set_transform_blend_table(tbtable)

        # Write the vertex data
        pacc_name = mesh_attribs['POSITION']
        pacc = gltf_data['accessors'][pacc_name]

        handle = vdata.modify_array(0).modify_handle()
        handle.unclean_set_num_rows(pacc['count'])

        buff_view = gltf_data['bufferViews'][pacc['bufferView']]
        buff = gltf_data['buffers'][buff_view['buffer']]
        buff_data = base64.b64decode(buff['uri'].split(',')[1])
        start = buff_view['byteOffset']
        end = buff_view['byteOffset'] + buff_view['byteLength']
        handle.copy_data_from(buff_data[start:end])
        handle = None
        #idx = start
        #while idx < end:
        #    s = struct.unpack_from('<ffffff', buff_data, idx)
        #    idx += 24
        #    print(s)

        # Write the transform blend table
        if is_skinned:
            tdata = GeomVertexWriter(vdata, InternalName.get_transform_blend())

            sacc = gltf_data['accessors'][mesh_attribs['WEIGHTS_0']]
            sbv = gltf_data['bufferViews'][sacc['bufferView']]
            sbuff = gltf_data['buffers'][sbv['buffer']]
            sbuff_data = base64.b64decode(sbuff['uri'].split(',')[1])

            for i in range(0, sbv['byteLength'], 32):
                joints = struct.unpack_from('<BBBB', sbuff_data, i)
                weights = struct.unpack_from('<ffff', sbuff_data, i+16)
                #print(i, joints, weights)
                tblend = TransformBlend()
                for j in range(4):
                    joint = joints[j]
                    weight = weights[j]
                    try:
                        jvt = jvtmap[joint]
                    except KeyError:
                        print("Could not find joint in jvtmap:\n\tjoint={}\n\tjvtmap={}".format(joint, jvtmap))
                        continue
                    tblend.add_transform(jvt, weight)
                tdata.add_data1i(tbtable.add_blend(tblend))

            tbtable.set_rows(SparseArray.lower_on(vdata.get_num_rows()))

        geom_idx = 0
        for gltf_primitive in gltf_mesh['primitives']:
            # Grab the index data
            prim = GeomTriangles(GeomEnums.UH_stream)

            iacc_name = gltf_primitive['indices']
            iacc = gltf_data['accessors'][iacc_name]

            num_verts = iacc['count']
            if iacc['componentType'] == 5123:
                prim.set_index_type(GeomEnums.NTUint16)
            else:
                prim.set_index_type(GeomEnums.NTUint32)
            handle = prim.modify_vertices(num_verts).modify_handle()
            handle.unclean_set_num_rows(num_verts)

            buff_view = gltf_data['bufferViews'][iacc['bufferView']]
            buff = gltf_data['buffers'][buff_view['buffer']]
            buff_data = base64.b64decode(buff['uri'].split(',')[1])
            start = buff_view['byteOffset']
            end = buff_view['byteOffset'] + buff_view['byteLength']
            handle.copy_data_from(buff_data[start:end])
            #idx = start
            #indbuf = []
            #while idx < end:
            #    s = struct.unpack_from('<HHH', buff_data, idx)
            #    idx += 6
            #    print(s)
            #print(prim.get_max_vertex(), vdata.get_num_rows())
            handle = None

            #ss = StringStream()
            #vdata.write(ss)
            #print(ss.getData())
            #prim.write(ss, 2)
            #print(ss.getData())

            # Get a material
            matid = gltf_primitive.get('material', None)
            if matid is None:
                print(
                    "Warning: mesh {} has a primitive with no material, using an empty RenderState"
                    .format(meshid)
                )
                mat = RenderState.make_empty()
            elif matid not in self.mat_states:
                print(
                    "Warning: material with name {} has no associated mat state, using an empty RenderState"
                    .format(matid)
                )
                mat = RenderState.make_empty()
            else:
                mat = self.mat_states[gltf_primitive['material']]
                self.mat_mesh_map[gltf_primitive['material']].append((meshid, geom_idx))

            # Now put it together
            geom = Geom(vdata)
            geom.add_primitive(prim)
            node.add_geom(geom, mat)

            geom_idx += 1

        self.meshes[meshid] = node

    def load_camera(self, camid, gltf_camera):
        camname = gltf_node.get('name', 'cam'+str(camid))
        node = self.cameras.get(camid, Camera(camname))

        if gltf_camera['type'] == 'perspective':
            gltf_lens = gltf_camera['perspective']
            lens = PerspectiveLens()
            lens.set_fov(math.degrees(gltf_lens['yfov'] * gltf_lens['aspectRatio']), math.degrees(gltf_lens['yfov']))
            lens.set_near_far(gltf_lens['znear'], gltf_lens['zfar'])
            lens.set_view_vector((0, 0, -1), (0, 1, 0))
            node.set_lens(lens)

        self.cameras[camid] = node

    def load_light(self, lightid, gltf_light):
        node = self.lights.get(lightid, None)
        lightname = gltf_node.get('name', 'light'+str(lightid))

        ltype = gltf_light['type']
        # Construct a new light if needed
        if node is None:
            if ltype == 'point':
                node = PointLight(lightname)
            elif ltype == 'directional':
                node = DirectionalLight(lightname)
                node.set_direction((0, 0, -1))
            elif ltype == 'spot':
                node = Spotlight(lightname)
            else:
                print("Unsupported light type for light with name {}: {}".format(lightname, gltf_light['type']))
                node = PandaNode(lightname)

        # Update the light
        if ltype == 'unsupported':
            lightprops = {}
        else:
            lightprops = gltf_light[ltype]

        if ltype in ('point', 'directional', 'spot'):
            node.set_color(LColor(*lightprops['color'], w=1))

        if ltype in ('point', 'spot'):
            att = LPoint3(
                lightprops['constantAttenuation'],
                lightprops['linearAttenuation'],
                lightprops['quadraticAttenuation']
            )
            node.set_attenuation(att)

        self.lights[lightid] = node


def main():
    import sys
    import json

    if len(sys.argv) < 2:
        print("Missing glTF srouce file argument")
        sys.exit(1)
    elif len(sys.argv) < 3 and not sys.argv[1].endswith('.gltf'):
        print("Missing bam destination file argument")
        sys.exit(1)

    infile = sys.argv[1]
    outfile = sys.argv[2] if len(sys.argv) > 2 else infile.replace('.gltf', '.bam')

    with open(infile) as gltf_file:
        gltf_data = json.load(gltf_file)

    dstfname = Filename.fromOsSpecific(outfile)
    get_model_path().prepend_directory(dstfname.getDirname())

    converter = Converter()
    converter.update(gltf_data, writing_bam=True)

    #converter.active_scene.ls()

    converter.active_scene.write_bam_file(dstfname)


if __name__ == '__main__':
    main()
