#!/usr/bin/env python3
# encoding: utf-8

"""
A module for performing various species-related format conversions.
"""

import numpy as np
import os

import pybel
from rdkit import Chem
from rdkit.Chem import rdMolTransforms as rdMT

from arkane.common import symbol_by_number, mass_by_symbol, get_element_mass
from rmgpy.exceptions import AtomTypeError
from rmgpy.molecule.molecule import Atom, Bond, Molecule
from rmgpy.quantity import ArrayQuantity
from rmgpy.species import Species
from rmgpy.statmech import Conformer

from arc.common import get_logger, is_str_float, get_atom_radius
from arc.exceptions import ConverterError, InputError, SanitizationError, SpeciesError
from arc.species.xyz_to_2d import MolGraph
from arc.species.zmat import zmat_to_coords, get_atom_indices_from_zmat_parameter, xyz_to_zmat, KEY_FROM_LEN, \
    get_parameter_from_atom_indices, get_all_neighbors


logger = get_logger()


def str_to_xyz(xyz_str):
    """
    Convert a string xyz format to the ARC dict xyz style.
    Note: The ``xyz_str`` argument could also direct to a file path to parse the data from.
    The xyz string format may have optional Gaussian-style isotope specification, e.g.::

        C(Iso=13)    0.6616514836    0.4027481525   -0.4847382281
        N           -0.6039793084    0.6637270105    0.0671637135
        H           -1.4226865648   -0.4973210697   -0.2238712255
        H           -0.4993010635    0.6531020442    1.0853092315
        H           -2.2115796924   -0.4529256762    0.4144516252
        H           -1.8113671395   -0.3268900681   -1.1468957003

    which will also be parsed into the ARC xyz dictionary format, e.g.::

        {'symbols': ('C', 'N', 'H', 'H', 'H', 'H'),
         'isotopes': (13, 14, 1, 1, 1, 1),
         'coords': ((0.6616514836, 0.4027481525, -0.4847382281),
                    (-0.6039793084, 0.6637270105, 0.0671637135),
                    (-1.4226865648, -0.4973210697, -0.2238712255),
                    (-0.4993010635, 0.6531020442, 1.0853092315),
                    (-2.2115796924, -0.4529256762, 0.4144516252),
                    (-1.8113671395, -0.3268900681, -1.1468957003))}

    Args:
        xyz_str (str): The string xyz format to be converted.

    Returns:
        dict: The ARC xyz format.

    Raises:
        ConverterError: If xyz_str is not a string or does not have four space-separated entries per non empty line.
    """
    if not isinstance(xyz_str, str):
        raise ConverterError(f'Expected a string input, got {type(xyz_str)}')
    if os.path.isfile(xyz_str):
        from arc.parser import parse_xyz_from_file
        return parse_xyz_from_file(xyz_str)
    xyz_str = xyz_str.replace(',', ' ')
    if len(xyz_str.splitlines()[0]) == 1:
        # this is a zmat
        return zmat_to_xyz(zmat=str_to_zmat(xyz_str), keep_dummy=False)
    xyz_dict = {'symbols': tuple(), 'isotopes': tuple(), 'coords': tuple()}
    if all([len(line.split()) == 6 for line in xyz_str.splitlines() if line.strip()]):
        # Convert Gaussian output format, e.g., "      1          8           0        3.132319    0.769111   -0.080869"
        # not considering isotopes in this method!
        for line in xyz_str.splitlines():
            if line.strip():
                splits = line.split()
                symbol = symbol_by_number[int(splits[1])]
                coord = (float(splits[3]), float(splits[4]), float(splits[5]))
                xyz_dict['symbols'] += (symbol,)
                xyz_dict['isotopes'] += (get_most_common_isotope_for_element(symbol),)
                xyz_dict['coords'] += (coord,)
    else:
        # this is a "regular" string xyz format, if it has isotope information it will be preserved
        for line in xyz_str.strip().splitlines():
            if line.strip():
                splits = line.split()
                if len(splits) != 4:
                    raise ConverterError(f'xyz_str has an incorrect format, expected 4 elements in each line, '
                                         f'got "{line}" in:\n{xyz_str}')
                symbol = splits[0]
                if '(iso=' in symbol.lower():
                    isotope = int(symbol.split('=')[1].strip(')'))
                    symbol = symbol.split('(')[0]
                else:
                    # no specific isotope is specified in str_xyz
                    isotope = get_most_common_isotope_for_element(symbol)
                coord = (float(splits[1]), float(splits[2]), float(splits[3]))
                xyz_dict['symbols'] += (symbol,)
                xyz_dict['isotopes'] += (isotope,)
                xyz_dict['coords'] += (coord,)
    return xyz_dict


def xyz_to_str(xyz_dict, isotope_format=None):
    """
    Convert an ARC xyz dictionary format, e.g.::

        {'symbols': ('C', 'N', 'H', 'H', 'H', 'H'),
         'isotopes': (13, 14, 1, 1, 1, 1),
         'coords': ((0.6616514836, 0.4027481525, -0.4847382281),
                    (-0.6039793084, 0.6637270105, 0.0671637135),
                    (-1.4226865648, -0.4973210697, -0.2238712255),
                    (-0.4993010635, 0.6531020442, 1.0853092315),
                    (-2.2115796924, -0.4529256762, 0.4144516252),
                    (-1.8113671395, -0.3268900681, -1.1468957003))}

    to a string xyz format with optional Gaussian-style isotope specification, e.g.::

        C(Iso=13)    0.6616514836    0.4027481525   -0.4847382281
        N           -0.6039793084    0.6637270105    0.0671637135
        H           -1.4226865648   -0.4973210697   -0.2238712255
        H           -0.4993010635    0.6531020442    1.0853092315
        H           -2.2115796924   -0.4529256762    0.4144516252
        H           -1.8113671395   -0.3268900681   -1.1468957003

    Args:
        xyz_dict (dict): The ARC xyz format to be converted.
        isotope_format (str, optional): The format for specifying the isotope if it is not the most abundant one.
                                        By default, isotopes will not be specified. Currently the only supported
                                        option is 'gaussian'.

    Returns:
        str: The string xyz format.

    Raises:
        ConverterError: If input is not a dict or does not have all attributes.
    """
    xyz_dict = check_xyz_dict(xyz_dict)
    if xyz_dict is None:
        logger.warning('Got None for xyz_dict')
        return None
    recognized_isotope_formats = ['gaussian']
    if any([key not in list(xyz_dict.keys()) for key in ['symbols', 'isotopes', 'coords']]):
        raise ConverterError(f'Missing keys in the xyz dictionary. Expected to find "symbols", "isotopes", and '
                             f'"coords", but got {list(xyz_dict.keys())} in\n{xyz_dict}')
    if any([len(xyz_dict['isotopes']) != len(xyz_dict['symbols']),
            len(xyz_dict['coords']) != len(xyz_dict['symbols'])]):
        raise ConverterError(f'Got different lengths for "symbols", "isotopes", and "coords": '
                             f'{len(xyz_dict["symbols"])}, {len(xyz_dict["isotopes"])}, and {len(xyz_dict["coords"])}, '
                             f'respectively, in xyz:\n{xyz_dict}')
    if any([len(xyz_dict['coords'][i]) != 3 for i in range(len(xyz_dict['coords']))]):
        raise ConverterError(f'Expected 3 coordinates for each atom (x, y, and z), got:\n{xyz_dict}')
    xyz_list = list()
    for symbol, isotope, coord in zip(xyz_dict['symbols'], xyz_dict['isotopes'], xyz_dict['coords']):
        common_isotope = get_most_common_isotope_for_element(symbol)
        if isotope_format is not None and common_isotope != isotope:
            # consider the isotope number
            if isotope_format == 'gaussian':
                element_with_isotope = '{0}(Iso={1})'.format(symbol, isotope)
                row = '{0:14}'.format(element_with_isotope)
            else:
                raise ConverterError('Recognized isotope formats for printing are {0}, got: {1}'.format(
                                      recognized_isotope_formats, isotope_format))
        else:
            # don't consider the isotope number
            row = '{0:4}'.format(symbol)
        row += '{0:14.8f}{1:14.8f}{2:14.8f}'.format(*coord)
        xyz_list.append(row)
    return '\n'.join(xyz_list)


def xyz_to_x_y_z(xyz_dict):
    """
    Get the X, Y, and Z coordinates separately from the ARC xyz dictionary format.

    Args:
        xyz_dict (dict): The ARC xyz format.

    Returns:
        tuple: The X coordinates.
    Returns:
        tuple: The Y coordinates.
    Returns:
        tuple: The Z coordinates.
    """
    xyz_dict = check_xyz_dict(xyz_dict)
    x, y, z = tuple(), tuple(), tuple()
    for coord in xyz_dict['coords']:
        x += (coord[0],)
        y += (coord[1],)
        z += (coord[2],)
    return x, y, z


def xyz_to_coords_list(xyz_dict):
    """
    Get the coords part of an xyz dict as a (mutable) list of lists (rather than a tuple of tuples)

    Args:
        xyz_dict (dict): The ARC xyz format.

    Returns:
        list: The coordinates.
    """
    xyz_dict = check_xyz_dict(xyz_dict)
    coords_tuple = xyz_dict['coords']
    coords_list = list()
    for coords_tup in coords_tuple:
        coords_list.append([coords_tup[0], coords_tup[1], coords_tup[2]])
    return coords_list


def xyz_to_xyz_file_format(xyz_dict, comment=''):
    """
    Get the `XYZ file format <https://en.wikipedia.org/wiki/XYZ_file_format>`_ representation
    from the ARC xyz dictionary format.
    This function does not consider isotopes.

    Args:
        xyz_dict (dict): The ARC xyz format.
        comment (str, optional): A comment to be shown in the output's 2nd line.

    Returns:
        str: The XYZ file format.

    Raises:
        ConverterError: If ``xyz_dict`` is of wrong format or ``comment`` is a multiline string.
    """
    xyz_dict = check_xyz_dict(xyz_dict)
    if len(comment.splitlines()) > 1:
        raise ConverterError('The comment attribute cannot be a multiline string, got:\n{0}'.format(list(comment)))
    return str(len(xyz_dict['symbols'])) + '\n' + comment.strip() + '\n' + xyz_to_str(xyz_dict) + '\n'


def xyz_file_format_to_xyz(xyz_file):
    """
    Get the ARC xyz dictionary format from an
    `XYZ file format <https://en.wikipedia.org/wiki/XYZ_file_format>`_ representation.

    Args:
        xyz_file (str): The content of an XYZ file

    Returns:
        dict: The ARC dictionary xyz format.

    Raises:
        ConverterError: If cannot identify the number of atoms entry, of if it is different that the actual number.
    """
    lines = xyz_file.strip().splitlines()
    if not lines[0].isdigit():
        raise ConverterError('Cannot identify the number of atoms from the XYZ file format representation. '
                             'Expected a number, got: {0} of type {1}'.format(lines[0], type(lines[0])))
    number_of_atoms = int(lines[0])
    lines = lines[2:]
    if len(lines) != number_of_atoms:
        raise ConverterError('The actual number of atoms ({0}) does not match the expected number parsed ({1}).'.format(
                              len(lines), number_of_atoms))
    xyz_str = '\n'.join(lines)
    return str_to_xyz(xyz_str)


def xyz_from_data(coords, numbers=None, symbols=None, isotopes=None):
    """
    Get the ARC xyz dictionary format from raw data.
    Either ``numbers`` or ``symbols`` must be specified.
    If ``isotopes`` isn't specified, the most common isotopes will be assumed for all elements.

    Args:
        coords (tuple, list): The xyz coordinates.
        numbers (tuple, list, optional): Element nuclear charge numbers.
        symbols (tuple, list, optional): Element symbols.
        isotopes (tuple, list, optional): Element isotope numbers.

    Returns:
        dict: The ARC dictionary xyz format.

    Raises:
        ConverterError: If neither ``numbers`` nor ``symbols`` are specified, if both are specified,
                        or if the input lengths aren't consistent.
    """
    if isinstance(coords, np.ndarray):
        coords = tuple(tuple(coord.tolist()) for coord in coords)
    elif isinstance(coords, list):
        coords = tuple(tuple(coord) for coord in coords)
    if numbers is not None and isinstance(numbers, np.ndarray):
        numbers = tuple(numbers.tolist())
    elif numbers is not None and isinstance(numbers, list):
        numbers = tuple(numbers)
    if symbols is not None and isinstance(symbols, list):
        symbols = tuple(symbols)
    if isotopes is not None and isinstance(isotopes, list):
        isotopes = tuple(isotopes)
    if not isinstance(coords, tuple):
        raise ConverterError('Expected coords to be a tuple, got {0} which is a {1}'.format(coords, type(coords)))
    if numbers is not None and not isinstance(numbers, tuple):
        raise ConverterError('Expected numbers to be a tuple, got {0} which is a {1}'.format(numbers, type(numbers)))
    if symbols is not None and not isinstance(symbols, tuple):
        raise ConverterError('Expected symbols to be a tuple, got {0} which is a {1}'.format(symbols, type(symbols)))
    if isotopes is not None and not isinstance(isotopes, tuple):
        raise ConverterError('Expected isotopes to be a tuple, got {0} which is a {1}'.format(isotopes, type(isotopes)))
    if numbers is None and symbols is None:
        raise ConverterError('Must set either "numbers" or "symbols". Got neither.')
    if numbers is not None and symbols is not None:
        raise ConverterError('Must set either "numbers" or "symbols". Got both.')
    if numbers is not None:
        symbols = tuple(symbol_by_number[number] for number in numbers)
    if len(coords) != len(symbols):
        raise ConverterError(f'The length of the coordinates ({len(coords)}) is different than the length of the '
                             f'numbers/symbols ({len(symbols)}).')
    if isotopes is not None and len(coords) != len(isotopes):
        raise ConverterError(f'The length of the coordinates ({len(coords)}) is different than the length of isotopes '
                             f'({len(isotopes)}).')
    if isotopes is None:
        isotopes = tuple(get_most_common_isotope_for_element(symbol) for symbol in symbols)
    xyz_dict = {'symbols': symbols, 'isotopes': isotopes, 'coords': coords}
    return xyz_dict


def rmg_conformer_to_xyz(conformer):
    """
    Convert xyz coordinates from an rmgpy.statmech.Conformer object into the ARC dict xyz style.

    Notes:
        Only the xyz information (symbols and coordinates) will be taken from the Conformer object. Other properties
        such as electronic energy will not be converted.

        We also assume that we can get the isotope number by rounding the mass

    Args:
        conformer (Conformer): An rmgpy.statmech.Conformer object containing the desired xyz coordinates

    Raises:
        TypeError: If conformer is not an rmgpy.statmech.Conformer object

    Returns:
        dict: The ARC xyz format
    """
    if not isinstance(conformer, Conformer):
        raise TypeError(f'Expected conformer to be an rmgpy.statmech.Conformer object but instead got {conformer}, '
                        f'which is a {type(conformer)} object.')

    symbols = tuple(symbol_by_number[n] for n in conformer.number.value)
    isotopes = tuple(int(round(m)) for m in conformer.mass.value)
    coords = tuple(tuple(coord) for coord in conformer.coordinates.value)

    xyz_dict = {'symbols': symbols, 'isotopes': isotopes, 'coords': coords}
    return xyz_dict


def xyz_to_rmg_conformer(xyz_dict):
    """
    Convert the Arc dict xyz style into an rmgpy.statmech.Conformer object containing these coordinates.

    Notes:
        Only the xyz information will be supplied to the newly created Conformer object

    Args:
        xyz_dict (dict): The ARC dict xyz style coordinates

    Returns:
        Conformer: An rmgpy.statmech.Conformer object containing the desired xyz coordinates
    """
    xyz_dict = check_xyz_dict(xyz_dict)
    mass_and_number = (get_element_mass(*args) for args in zip(xyz_dict['symbols'], xyz_dict['isotopes']))
    mass, number = zip(*mass_and_number)
    mass = ArrayQuantity(mass, 'amu')
    number = ArrayQuantity(number, '')
    coordinates = ArrayQuantity(xyz_dict['coords'], 'angstroms')

    conformer = Conformer(number=number, mass=mass, coordinates=coordinates)
    return conformer


def standardize_xyz_string(xyz_str, isotope_format=None):
    """
    A helper function to correct xyz string format input (** string to string **).
    Usually empty lines are added by the user either in the beginning or the end,
    here we remove them along with other common issues.

    Args:
        xyz_str (str): The string xyz format, or a Gaussian output format.
        isotope_format (str, optional): The format for specifying the isotope if it is not the most abundant one.
                                        By default, isotopes will not be specified. Currently the only supported
                                        option is 'gaussian'.

    Returns:
        str: The string xyz format in standardized format.

    Raises:
        ConverterError: If ``xyz_str`` is of wrong type.
    """
    if not isinstance(xyz_str, str):
        raise ConverterError('Expected a string format, got {0}'.format(type(xyz_str)))
    xyz_dict = str_to_xyz(xyz_str)
    return xyz_to_str(xyz_dict=xyz_dict, isotope_format=isotope_format)


def check_xyz_dict(xyz):
    """
    Check that the xyz dictionary entered is valid.
    If it is a string, convert it.
    If it is a Z matrix, convert it to cartesian coordinates,
    If isotopes are not in xyz_dict, common values will be added.

    Args:
        xyz (dict, str): The xyz dictionary.

    Raises:
        ConverterError: If ``xyz`` is of wrong type or is missing symbols or coords.
    """
    xyz_dict = str_to_xyz(xyz) if isinstance(xyz, str) else xyz
    if not isinstance(xyz_dict, dict):
        raise ConverterError(f'Expected a dictionary, got {type(xyz_dict)}')
    if 'vars' in list(xyz_dict.keys()):
        # this is a zmat, convert to cartesian
        xyz_dict = zmat_to_xyz(zmat=xyz_dict, keep_dummy=False)
    if 'symbols' not in list(xyz_dict.keys()):
        raise ConverterError(f'XYZ dictionary is missing symbols. Got:\n{xyz_dict}')
    if 'coords' not in list(xyz_dict.keys()):
        raise ConverterError(f'XYZ dictionary is missing coords. Got:\n{xyz_dict}')
    if len(xyz_dict['symbols']) != len(xyz_dict['coords']):
        raise ConverterError(f'Got {len(xyz_dict["symbols"])} symbols and {len(xyz_dict["coords"])} '
                             f'coordinates:\n{xyz_dict}')
    if 'isotopes' not in list(xyz_dict.keys()):
        xyz_dict = xyz_from_data(coords=xyz_dict['coords'], symbols=xyz_dict['symbols'])
    if len(xyz_dict['symbols']) != len(xyz_dict['isotopes']):
        raise ConverterError(f'Got {len(xyz_dict["symbols"])} symbols and {len(xyz_dict["isotopes"])} '
                             f'isotopes:\n{xyz_dict}')
    return xyz_dict


def check_zmat_dict(zmat):
    """
    Check that the zmat dictionary entered is valid.
    If it is a string, convert it.
    If it represents cartesian coordinates, convert it to internal coordinates.
    If a map isn't given, a trivial one will be added.

    Args:
        zmat (dict, str): The zmat dictionary.

    Raises:
        ConverterError: If ``zmat`` is of wrong type or is missing vars or coords.
    """
    zmat_dict = str_to_zmat(zmat) if isinstance(zmat, str) else zmat
    if not isinstance(zmat_dict, dict):
        raise ConverterError(f'Expected a dictionary, got {type(zmat_dict)}')
    if 'vars' not in list(zmat_dict.keys()):
        # this is probably a representation of cartesian coordinates, convert to zmat
        zmat_dict = zmat_from_xyz(xyz=check_xyz_dict(zmat_dict), consolidate=True)
    if 'symbols' not in list(zmat_dict.keys()):
        raise ConverterError(f'zmat dictionary is missing symbols. Got:\n{zmat_dict}')
    if 'coords' not in list(zmat_dict.keys()):
        raise ConverterError(f'zmat dictionary is missing coords. Got:\n{zmat_dict}')
    if len(zmat_dict['symbols']) != len(zmat_dict['coords']):
        raise ConverterError(f'Got {len(zmat_dict["symbols"])} symbols and {len(zmat_dict["coords"])} '
                             f'coordinates:\n{zmat_dict}')
    if 'map' not in list(zmat_dict.keys()):
        # add a trivial map
        zmat_dict['map'] = {i: i for i in range(len(zmat_dict['symbols']))}
    if len(zmat_dict['symbols']) != len(zmat_dict['map']):
        raise ConverterError(f'Got {len(zmat_dict["symbols"])} symbols and {len(zmat_dict["isotopes"])} '
                             f'isotopes:\n{zmat_dict}')
    for i, coord in enumerate(zmat_dict['coords']):
        for j, param in enumerate(coord):
            if param is not None:
                indices = get_atom_indices_from_zmat_parameter(param)
                if not any(i == index_tuple[0] for index_tuple in indices):
                    raise ConverterError(f'The {param} parameter in the zmat is ill-defined:\n{zmat_dict}')
            if (i == 0 or i == 1 and j in [1, 2] or i == 2 and j == 3) and param is not None:
                raise ConverterError(f'The zmat is ill-defined:\n{zmat_dict}')
    return zmat_dict


def remove_dummies(xyz):
    """
    Remove dummy ('X') atoms from cartesian coordinates.

    Args:
        xyz (dict, str): The cartesian coordinate, either in a dict or str format.

    Returns:
        dict: The coordinates w/o dummy atoms.

    Raises:
        InputError: If ``xyz`` if of wrong type.
    """
    if isinstance(xyz, str):
        xyz = str_to_xyz(xyz)
    if not isinstance(xyz, dict):
        raise InputError(f'xyz must be a dictionary, got {type(xyz)}')
    symbols, isotopes, coords = list(), list(), list()
    for symbol, isotope, coord in zip(xyz['symbols'], xyz['isotopes'], xyz['coords']):
        if symbol != 'X':
            symbols.append(symbol)
            isotopes.append(isotope)
            coords.append(coord)
    return xyz_from_data(coords=coords, symbols=symbols, isotopes=isotopes)


def zmat_from_xyz(xyz, mol=None, constraints=None, consolidate=True, consolidation_tols=None):
    """
    Generate a Z matrix from xyz.

    Args:
        xyz (dict, str): The cartesian coordinate, either in a dict or str format.
        mol (Molecule, optional): The corresponding RMG Molecule with connectivity information.
        constraints (dict, optional): Accepted keys are:
                                      'R_atom', 'R_group', 'A_atom', 'A_group', 'D_atom', 'D_group', or 'D_groups'.
                                      'R', 'A', and 'D', constraining distances, angles, and dihedrals, respectively.
                                      Values are lists of atom indices (0-indexed) tuples.
                                      The atom indices order matters.
                                      Specifying '_atom' will cause only the last atom in the specified list values
                                      to translate/rotate if the corresponding zmat parameter is changed.
                                      Specifying '_group' will cause the entire group connected to the last atom
                                      to translate/rotate if the corresponding zmat parameter is changed.
                                      Specifying '_groups' (only valid for D) will cause the groups connected to
                                      the last two atoms to translate/rotate if the corresponding parameter is changed.
        consolidate (bool, optional): Whether to consolidate the zmat after generation, ``True`` to consolidate.
        consolidation_tols (dict, optional): Keys are 'R', 'A', 'D', values are floats representing absolute tolerance
                                             for consolidating almost equal internal coordinates.

    Returns:
        dict: The Z matrix.

    Raises:
        InputError: If ``xyz`` if of wrong type.
    """
    if isinstance(xyz, str):
        xyz = str_to_xyz(xyz)
    if not isinstance(xyz, dict):
        raise InputError(f'xyz must be a dictionary, got {type(xyz)}')
    xyz = remove_dummies(xyz)
    return xyz_to_zmat(xyz, mol=mol, constraints=constraints, consolidate=consolidate,
                       consolidation_tols=consolidation_tols)


def zmat_to_xyz(zmat, keep_dummy=False, xyz_isotopes=None):
    """
    Generate the xyz dict coordinates from a zmat dict.
    Most common isotopes assumed, unless a reference xyz dict is given.

    Args:
        zmat (dict): The zmat.
        keep_dummy (bool): Whether to keep dummy atoms ('X'), ``True`` to keep, default is ``False``.
        xyz_isotopes (dict): A reference xyz dictionary to take isotope information from.
                             Must be ordered as the original mol/xyz used to create ``zmat``.

    Returns:
        dict: The xyz cartesian coordinates.
    """
    coords, symbols = zmat_to_coords(zmat, keep_dummy=keep_dummy)
    isotopes = xyz_isotopes['isotopes'] if xyz_isotopes is not None else None
    xyz_dict = translate_to_center_of_mass(xyz_from_data(coords=coords, symbols=symbols, isotopes=isotopes))
    return xyz_dict


def zmat_to_str(zmat, zmat_format='gaussian', consolidate=True):
    """
    Convert a zmat to a string format.

    Args:
        zmat (dict): The Z Matrix to convert.
        zmat_format (str, optional): The requested format to output (varies by ESS).
                                     Allowed values are: 'gaussian', 'qchem', 'molpro', 'orca', or 'psi4'.
                                     The default format is 'gaussian'.
        consolidate (bool): Whether to return a consolidated zmat (geometry optimization will be more efficient).

    Returns:
        str: The string representation of the zmat in the requested format.

    Raises:
        ConverterError: If ``zmat`` is of wrong type or missing keys, or if ``zmat_format`` is not recognized.
    """
    if not isinstance(zmat, dict):
        raise ConverterError(f'zmat has to be a dict, got: {type(zmat)}')
    if 'symbols' not in zmat or 'coords' not in zmat or 'vars' not in zmat:
        raise ConverterError(f'zmat must contain the "symbols", "coords", and "vars" keys, got: '
                             f'{list(zmat.keys())}.')
    if zmat_format == 'terachem':
        raise ConverterError('TeraChem does not accept a zmat as input (it has its own internal conversions).')
    if zmat_format not in ['gaussian', 'qchem', 'molpro', 'orca', 'psi4']:
        raise ConverterError(f'zmat_format must be either gaussian, qchem, molpro, orca, or psi4, got: {zmat_format}.')
    if zmat_format == 'orca':
        # replace dummy atom symbols
        symbols = list()
        for symbol in zmat['symbols']:
            symbols.append(symbol if symbol != 'X' else 'DA')
    else:
        symbols = zmat['symbols']
    if zmat_format == 'orca':
        # Redundant internal coordinates are automatically used by Orca,
        # parametarized internal coordinates are hence not supported
        consolidate = False
    separator = ',' if zmat_format in ['molpro'] else ''
    var_separator = '=' if zmat_format in ['gaussian', 'molpro', 'qchem', 'psi4'] else ' '
    zmat_str, variables_str, variables = '', '', list()
    type_indices = {'R': 1, 'A': 1, 'D': 1}  # consolidation counters
    variables_dict = dict()  # keys are coord (e.g., 'R_2|4_0|0'), values are vars (e.g., 'R1')
    for i, (symbol, coords) in enumerate(zip(symbols, zmat['coords'])):
        line = f'{symbol:>3}'
        for coord in coords:
            if coord is not None:
                index_tuples = get_atom_indices_from_zmat_parameter(coord)
                for indices in index_tuples:
                    if indices[0] == i:
                        break
                if consolidate:
                    if coord in list(variables_dict.keys()):
                        var_str = variables_dict[coord]
                    else:
                        var_type = coord[0]  # 'R', 'A', or 'D'
                        var_str = f'{var_type}{type_indices[var_type]}'
                        type_indices[var_type] += 1
                        variables_dict[coord] = var_str
                        variables.append(f'{var_str}{var_separator}{zmat["vars"][coord]:.4f}\n')
                    line += f'{separator}{indices[-1] + 1:8d}{separator}{var_str:>8}'  # convert to 1-indexed
                else:
                    line += f'{separator}{indices[-1] + 1:8d}{separator}{zmat["vars"][coord]:10.4f}'
        if zmat_format == 'orca' and consolidate:
            symbol, indices, coordinates = '', '', ''
            for j, entry in enumerate(line.split()):
                if j == 0:
                    symbol = entry + ' '
                elif j % 2 == 0:
                    coordinates += entry + ' '
                else:
                    indices += entry + ' '
            while len(indices.split()) < 3:
                indices += '0 '
            while len(coordinates.split()) < 3:
                coordinates += '0.0 '
            line = symbol + indices + coordinates[:-1]
        zmat_str += line + '\n'
    if zmat_format in ['gaussian']:
        variables_str = ''.join(sorted(variables))
        result = f'{zmat_str}Variables:\n{variables_str}' if consolidate else zmat_str
    elif zmat_format in ['qchem', 'psi4', 'orca']:
        variables_str = ''.join(sorted(variables))
        result = f'{zmat_str}\n{variables_str}' if consolidate else zmat_str
    elif zmat_format in ['molpro']:
        variables_str = ''.join(sorted(variables))
        result = f'{variables_str}\n{zmat_str}' if consolidate else zmat_str
    else:
        result = zmat_str + variables_str
    return result


def str_to_zmat(zmat_str):
    """
    Convert a string Z Matrix format to the ARC dict zmat style.
    Note: The ``zmat_str`` argument could also direct to a file path to parse the data from.
    A typical zmat string format may look like this::

          C
          H       1      R1
          H       1      R1       2      A1
          H       1      R1       2      A1       3      D1
          H       1      R1       2      A1       3      D2
          A1=109.4712
          D1=120.0000
          D2=240.0000
          R1=1.0912

    The resulting zmat for the above example is::

        {'symbols': ('C', 'H', 'H', 'H', 'H'),
         'coords': ((None, None, None),
                    ('R_1_0', None, None),
                    ('R_2_1', 'A_2_1_0', None),
                    ('R_3_2', 'A_3_2_0', 'D_3_2_0_1'), ('R_4_3', 'A_4_3_0', 'D_4_3_0_2')),
         'vars': {'R_1_0': 1.0912, 'R_2_1': 1.782, 'A_2_1_0': 35.2644, 'R_3_2': 1.782, 'A_3_2_0': 35.2644,
                  'D_3_2_0_1': 120.0, 'R_4_3': 1.782, 'A_4_3_0': 35.2644, 'D_4_3_0_2': 240.0},
         'map': {0: 0, 1: 1, 2: 2, 3: 3, 4: 4}}

    Args:
        zmat_str (str): The string zmat format to be converted.

    Returns:
        dict: The ARC zmat format.

    Raises:
        ConverterError: If zmat_str is not a string or does not have enough values per line.
    """
    if not isinstance(zmat_str, str):
        raise ConverterError(f'Expected a string input, got {type(zmat_str)}')
    if os.path.isfile(zmat_str):
        with open(zmat_str, 'r') as f:
            zmat_str = f.read()
    symbols, coords, variables = list(), list(), dict()
    coords_str = split_str_zmat(zmat_str)[0]
    index = 1
    for i, line in enumerate(coords_str.splitlines()):
        splits = line.split()
        if i == 1:
            # the atom index in this line must point to the first atom in the zmat,
            # deduce whether this zmat is in a 0-index or a 1-index format
            index = int(splits[1])
        if len(splits) not in (1, 3, 5, 7):
            raise ConverterError(f'Could not interpret the zmat line {line}')
        symbols.append(splits[0])
        r_key = f'R_{i}_{int(splits[1]) - index}' if len(splits) >= 3 else None  # convert to 0-index
        a_key = 'A' + r_key[1:] + f'_{int(splits[3]) - index}' if len(splits) >= 5 else None
        d_key = 'D' + a_key[1:] + f'_{int(splits[5]) - index}' if len(splits) == 7 else None
        coords.append((r_key, a_key, d_key))
        if r_key is not None:
            variables[r_key] = float(splits[2]) if is_str_float(splits[2]) \
                else get_zmat_str_var_value(zmat_str, splits[2])
        if a_key is not None:
            variables[a_key] = float(splits[4]) if is_str_float(splits[4]) \
                else get_zmat_str_var_value(zmat_str, splits[4])
        if d_key is not None:
            variables[d_key] = float(splits[6]) if is_str_float(splits[6]) \
                else get_zmat_str_var_value(zmat_str, splits[6])
    map_ = {i: i for i in range(len(symbols))}  # trivial map
    zmat_dict = {'symbols': tuple(symbols), 'coords': tuple(coords), 'vars': variables, 'map': map_}
    return zmat_dict


def split_str_zmat(zmat_str):
    """
    Split a string zmat into its coordinates and variables sections.

    Args:
        zmat_str (str): The zmat.

    Returns:
        str: The coords section.
    Returns:
        str: The variables section if it exists, else None.
    """
    coords, variables = list(), list()
    flag = False
    if 'variables' in zmat_str.lower():
        for line in zmat_str.splitlines():
            if 'variables' in line.lower():
                flag = True
                continue
            elif flag and line:
                variables.append(line)
            elif line:
                coords.append(line)
    else:
        splits = zmat_str.splitlines()
        if len(splits[0].split()) == len(splits[1].split()) and \
                (len(splits[0].split()) == 2 or (len(splits[0].split()) == 1 and len(splits[1]) != 1)):
            # this string starts with the variables section
            for line in splits:
                if flag and line:
                    coords.append(line)
                if not flag and len(line.split()) == len(splits[0].split()) and line:
                    variables.append(line)
                else:
                    flag = True
        elif len(splits[-1].split()) == len(splits[-2].split()) and len(splits[-1].split()) in [1, 2]:
            # this string starts with the coordinates section
            for line in splits:
                if flag and len(line.split()) == len(splits[-1].split()) and line:
                    variables.append(line)
                if not flag and line:
                    coords.append(line)
                else:
                    flag = True
    coords = '\n'.join(coords) if len(coords) else zmat_str
    variables = '\n'.join(variables) if len(variables) else None
    return coords, variables


def get_zmat_str_var_value(zmat_str, var):
    """
    Returns the value of a zmat variable from a string-represented zmat.

    Args:
        zmat_str (str): The string representation of the zmat.
        var (str): The variable to look for.

    Returns:
        float: The value corresponding to the ``var``.
    """
    for line in reversed(zmat_str.splitlines()):
        if var in line and len(line.split()) in [1, 2]:
            return float(line.replace('=', ' ').split()[-1])
    raise ConverterError(f'Could not find var "{var}" in zmat:\n{zmat_str}')


def get_zmat_param_value(coords, indices, mol):
    """
    Generates a zmat similarly to modify_coords(),
    but instead of modifying it, only reports on the value of a requested parameter.

    Args:
        coords (dict): Either cartesian (xyz) or internal (zmat) coordinates.
        indices (list): The indices to change (0-indexed). Specifying a list of length 2, 3, or 4
                        will result in a length, angle, or a dihedral angle parameter, respectively.
        mol (Molecule, optional): The corresponding RMG molecule with the connectivity information.

    Returns:
        float: The parameter value in Angstrom or degrees.
    """
    modification_type = KEY_FROM_LEN[len(indices)] + '_' + 'atom'  # e.g., R_atom
    if 'map' in list(coords.keys()):
        # a zmat was given, we actually need xyz to recreate a specific zmat for the parameter modification.
        xyz = zmat_to_xyz(zmat=coords)
    else:
        # coords represents xyz
        xyz = coords

    constraints = {modification_type: [tuple(indices)]}
    zmat = xyz_to_zmat(xyz=xyz, mol=mol, consolidate=False, constraints=constraints)
    param = get_parameter_from_atom_indices(zmat, indices, xyz_indexed=True)
    if isinstance(param, str):
        return zmat["vars"][param]
    elif isinstance(param, list):
        return sum(zmat["vars"][par] for par in param)


def modify_coords(coords, indices, new_value, modification_type, mol=None):
    """
    Modify either a bond length, angle, or dihedral angle in the given coordinates.
    The coordinates input could either be cartesian (preferred) or internal (will be first converter to cartesian,
    then to internal back again since a specific zmat must be created).
    Internal coordinates will be used for the modification (using folding and unfolding).

    Specifying an 'atom' modification type will only translate/rotate the atom represented by the first index
    if the corresponding zmat parameter is changed.
    Specifying a 'group' modification type will cause the entire group connected to the first atom to translate/rotate
    if the corresponding zmat parameter is changed.
    Specifying a 'groups' modification type (only valid for dihedral angles) will cause the groups connected to
    the first two atoms to translate/rotate if the corresponding zmat parameter is changed.

    Args:
        coords (dict): Either cartesian (xyz) or internal (zmat) coordinates.
        indices (list): The indices to change (0-indexed). Specifying a list of length 2, 3, or 4
                        will result in changing a bond length, angle, or a dihedral angle, respectively.
        new_value (float): The new value to set (in Angstrom or degrees).
        modification_type (str): Either 'atom', 'group', or 'groups' ('groups' is only allowed for dihedral angles).
                                 Note that D 'groups' is a composite constraint, equivalent to calling D 'group'
                                 for each 1st neighboring  atom in a torsion top.
        mol (Molecule, optional): The corresponding RMG molecule with the connectivity information.
                                  Mandatory if the modification type is 'group' or 'groups'.

    Returns:
        dict: The respective cartesian (xyz) coordinates reflecting the desired modification.

    Raises:
        InputError: If a group/s modification type is requested but ``mol`` is ``None``,
                    or if a 'groups' modification type was specified for R or A.
    """
    if modification_type not in ['atom', 'group', 'groups']:
        raise InputError(f'Allowed modification types are atom, group, or groups, got: {modification_type}.')
    if mol is None and 'group' in modification_type:
        raise InputError(f'A molecule must be given for a {modification_type} modification type.')
    modification_type = KEY_FROM_LEN[len(indices)] + '_' + modification_type  # e.g., R_group
    if modification_type == 'groups' and modification_type[0] != 'D':
        raise InputError(f'The "groups" modification type is only supported for dihedrals (D), '
                         f'not for an {modification_type[0]} parameter.')
    if 'map' in list(coords.keys()):
        # a zmat was given, we actually need xyz to recreate a specific zmat for the parameter modification.
        xyz = zmat_to_xyz(zmat=coords)
    else:
        # coords represents xyz
        xyz = coords

    constraints_list = [tuple(indices)]
    if modification_type == 'D_groups':
        # this is a special constraint for which neighbor dihedrals must be considered as well.
        neighbors = get_all_neighbors(mol=mol, atom_index=indices[1])
        for neighbor in neighbors:
            if neighbor not in indices:
                constraints_list.append(tuple([neighbor] + indices[1:]))
        modification_type = 'D_group'

    new_xyz = xyz
    increment = None
    for constraint in constraints_list:
        constraint_dict = {modification_type: [constraint]}
        zmat = xyz_to_zmat(xyz=new_xyz, mol=mol, consolidate=False, constraints=constraint_dict)
        param = get_parameter_from_atom_indices(zmat, constraint, xyz_indexed=True)

        if modification_type == 'D_group' and increment is None:
            # determine the dihedral increment, will be used for all other D-group modifications
            increment = new_value - zmat["vars"][param]
        if isinstance(param, str):
            if increment is None:
                zmat['vars'][param] = new_value
            else:
                zmat['vars'][param] += increment
        elif isinstance(param, list):
            # the requested parameter represents an angle split by a dummy atom,
            # a list of the two corresponding parameters was be returned
            zmat['vars'][param[0]] = new_value - sum(zmat['vars'][par] for par in param[1:])
        new_xyz = zmat_to_xyz(zmat=zmat)
    return new_xyz


def get_most_common_isotope_for_element(element_symbol):
    """
    Get the most common isotope for a given element symbol.

    Args:
        element_symbol (str): The element symbol.

    Returns:
        int: The most common isotope number for the element.
             Returns ``None`` for dummy atoms ('X').
    """
    if element_symbol == 'X':
        # this is a dummy atom (such as in a zmat)
        return None
    mass_list = mass_by_symbol[element_symbol]
    if len(mass_list[0]) == 2:
        # isotope contribution is unavailable, just get the first entry
        isotope = mass_list[0][0]
    else:
        # isotope contribution is unavailable, get the most common isotope
        isotope, isotope_contribution = mass_list[0][0], mass_list[0][2]
        for iso in mass_list:
            if iso[2] > isotope_contribution:
                isotope_contribution = iso[2]
                isotope = iso[0]
    return isotope


def xyz_to_pybel_mol(xyz):
    """
    Convert xyz into an Open Babel molecule object.

    Args:
        xyz (dict): ARC's xyz dictionary format.

    Returns:
        OBmol: An Open Babel molecule.
    """
    xyz = check_xyz_dict(xyz)
    try:
        pybel_mol = pybel.readstring('xyz', xyz_to_xyz_file_format(xyz))
    except (IOError, InputError):
        return None
    return pybel_mol


def pybel_to_inchi(pybel_mol, has_h=True):
    """
    Convert an Open Babel molecule object to InChI

    Args:
        pybel_mol (OBmol): An Open Babel molecule.
        has_h (bool): Whether the molecule has hydrogen atoms. ``True`` if it does.

    Returns:
        str: The respective InChI representation of the molecule.
    """
    if has_h:
        inchi = pybel_mol.write('inchi', opt={'F': None}).strip()  # Add fixed H layer
    else:
        inchi = pybel_mol.write('inchi').strip()
    return inchi


def rmg_mol_from_inchi(inchi):
    """
    Generate an RMG Molecule object from InChI.

    Args:
        inchi (str): The InChI string.

    Returns:
        Molecule: The respective RMG Molecule object.
    """
    try:
        rmg_mol = Molecule().from_inchi(inchi)
    except (AtomTypeError, ValueError, KeyError, TypeError) as e:
        logger.warning('Got the following Error when trying to create an RMG Molecule object from InChI:'
                       '\n{0}'.format(e))
        return None
    return rmg_mol


def elementize(atom):
    """
    Convert the atom type of an RMG:Atom object into its general parent element atom type (e.g., `S4d` into `S`).

    Args:
        atom (Atom): The atom to process.
    """
    atom_type = atom.atomtype
    atom_type = [at for at in atom_type.generic if at.label != 'R' and at.label != 'R!H' and 'Val' not in at.label]
    if atom_type:
        atom.atomtype = atom_type[0]


def molecules_from_xyz(xyz, multiplicity=None, charge=0):
    """
    Creating RMG:Molecule objects from xyz with correct atom labeling.
    Based on the MolGraph.perceive_smiles method.
    If `multiplicity` is given, the returned species multiplicity will be set to it.

    Args:
        xyz (dict): The ARC dict format xyz coordinates of the species.
        multiplicity (int, optional): The species spin multiplicity.
        charge (int, optional): The species net charge.

    Returns:
        Molecule: The respective Molecule object with only single bonds.
    Returns:
        Molecule: The respective Molecule object with perceived bond orders.
                  Returns None if unsuccessful to infer bond orders.
    """
    if xyz is None:
        return None, None
    xyz = check_xyz_dict(xyz)
    mol_bo = None

    # 1. Generate a molecule with no bond order information with atoms ordered as in xyz
    mol_graph = MolGraph(symbols=xyz['symbols'], coords=xyz['coords'])
    inferred_connections = mol_graph.infer_connections()
    if inferred_connections:
        mol_s1 = mol_graph.to_rmg_mol()  # An RMG Molecule with single bonds, atom order corresponds to xyz
    else:
        mol_s1 = s_bonds_mol_from_xyz(xyz)  # An RMG Molecule with single bonds, atom order corresponds to xyz
    if mol_s1 is None:
        logger.error(f'Could not create a 2D graph representation from xyz:\n{xyz_to_str(xyz)}')
        return None, None
    if multiplicity is not None:
        mol_s1.multiplicity = multiplicity
    mol_s1_updated = update_molecule(mol_s1, to_single_bonds=True)

    # 2. A. Generate a molecule with bond order information using pybel:
    pybel_mol = xyz_to_pybel_mol(xyz)
    if pybel_mol is not None:
        inchi = pybel_to_inchi(pybel_mol, has_h=bool(len([atom.is_hydrogen() for atom in mol_s1_updated.atoms])))
        mol_bo = rmg_mol_from_inchi(inchi)  # An RMG Molecule with bond orders, but without preserved atom order

    # TODO 2. B. Deduce bond orders from xyz distances (fallback method)
    # else:
    #     mol_bo = deduce_bond_orders_from_distances(xyz)

    if mol_bo is not None:
        if multiplicity is not None:
            try:
                set_multiplicity(mol_bo, multiplicity, charge)
            except SpeciesError as e:
                logger.warning('Cannot infer 2D graph connectivity, failed to set species multiplicity with the '
                               'following error:\n{0}'.format(e))
                return mol_s1_updated, None
        mol_s1_updated.multiplicity = mol_bo.multiplicity
        try:
            order_atoms(ref_mol=mol_s1_updated, mol=mol_bo)
        except SanitizationError:
            logger.warning('Could not order atoms for {0}!'.format(mol_s1_updated.copy(deep=True).to_smiles()))
        try:
            set_multiplicity(mol_s1_updated, mol_bo.multiplicity, charge, radical_map=mol_bo)
        except SpeciesError as e:
            logger.warning('Cannot infer 2D graph connectivity, failed to set species multiplicity with the '
                           'following error:\n{0}'.format(e))
            return mol_s1_updated, mol_bo

    return mol_s1_updated, mol_bo


def set_multiplicity(mol, multiplicity, charge, radical_map=None):
    """
    Set the multiplicity and charge of a molecule.
    If a `radical_map`, which is an RMG Molecule object with the same atom order, is given,
    it'll be used to set radicals (useful if bond orders aren't known for a molecule).

    Args:
        mol (Molecule): The RMG Molecule object.
        multiplicity (int) The spin multiplicity.
        charge (int): The species net charge.
        radical_map (Molecule, optional): An RMG Molecule object with the same atom order to be used as a radical map.

    Raises:
        ConverterError: If ``radical_map`` is of wrong type.
        SpeciesError: If number of radicals and multiplicity do not match or if connectivity cannot be inferred.
    """
    mol.multiplicity = multiplicity
    if radical_map is not None:
        if not isinstance(radical_map, Molecule):
            raise ConverterError(f'radical_map sent to set_multiplicity() has to be a Molecule object. '
                                 f'Got {type(radical_map)}')
        set_radicals_by_map(mol, radical_map)
    radicals = mol.get_radical_count()
    if mol.multiplicity != radicals + 1:
        # this is not the trivial "multiplicity = number of radicals + 1" case
        # either the number of radicals was not identified correctly from the 3D structure (i.e., should be lone pairs),
        # or their spin isn't determined correctly
        if mol.multiplicity > radicals + 1:
            # there are sites that should have radicals, but weren't identified as such.
            # try adding radicals according to missing valances
            add_rads_by_atom_valance(mol)
            if mol.multiplicity > radicals + 1:
                # still problematic, currently there's no automated solution to this case, raise an error
                raise SpeciesError('A multiplicity of {0} was given, but only {1} radicals were identified. '
                                   'Cannot infer 2D graph representation for this species.\nMore info:{2}\n{3}'.format(
                                    mol.multiplicity, radicals, mol.to_smiles(), mol.to_adjacency_list()))
    add_lone_pairs_by_atom_valance(mol)
    # final check: an even number of radicals results in an odd multiplicity, and vice versa
    if divmod(mol.multiplicity, 2)[1] == divmod(radicals, 2)[1]:
        if not charge:
            raise SpeciesError('Number of radicals ({0}) and multiplicity ({1}) for {2} do not match.\n{3}'.format(
                radicals, mol.multiplicity, mol.to_smiles(), mol.to_adjacency_list()))
        else:
            logger.warning('Number of radicals ({0}) and multiplicity ({1}) for {2} do not match. It might be OK since '
                           'this species is charged and charged molecules are currently not perceived well in ARC.'
                           '\n{3}'.format(radicals, mol.multiplicity, mol.copy(deep=True).to_smiles(),
                                          mol.copy(deep=True).to_adjacency_list()))


def add_rads_by_atom_valance(mol):
    """
    A helper function for assigning radicals if not identified automatically,
    and they are missing according to the given multiplicity.
    We assume here that all partial charges were already set, but this assumption could be wrong.
    Note: This implementation might also be problematic for aromatic species with undefined bond orders.

    Args:
        mol (Molecule): The Molecule object to process.
    """
    for atom in mol.atoms:
        if atom.is_non_hydrogen():
            atomic_orbitals = atom.lone_pairs + atom.radical_electrons + atom.get_total_bond_order()
            missing_electrons = 4 - atomic_orbitals
            if missing_electrons:
                atom.radical_electrons = missing_electrons


def add_lone_pairs_by_atom_valance(mol):
    """
     helper function for assigning lone pairs instead of carbenes/nitrenes if not identified automatically,
    and they are missing according to the given multiplicity.

    Args:
        mol (Molecule): The Molecule object to process.
    """
    radicals = mol.get_radical_count()
    if mol.multiplicity < radicals + 1:
        carbenes, nitrenes = 0, 0
        for atom in mol.atoms:
            if atom.is_carbon() and atom.radical_electrons >= 2:
                carbenes += 1
            elif atom.is_nitrogen() and atom.radical_electrons >= 2:
                nitrenes += 1
        if 2 * (carbenes + nitrenes) + mol.multiplicity == radicals + 1:
            # this issue can be solved by converting carbenes/nitrenes to lone pairs:
            if carbenes:
                for i in range(len(mol.atoms)):
                    atom = mol.atoms[i]
                    if atom.is_carbon() and atom.radical_electrons >= 2:
                        atom.lone_pairs += 1
                        atom.radical_electrons -= 2
            if nitrenes:
                for i in range(len(mol.atoms)):
                    atom = mol.atoms[i]
                    if atom.is_nitrogen() and atom.radical_electrons >= 2:
                        for atom2, bond12 in atom.edges.items():
                            if atom2.is_sulfur() and atom2.lone_pairs >= 2 and bond12.is_single():
                                bond12.set_order_num(3)
                                atom2.lone_pairs -= 1
                                break
                            elif atom2.is_sulfur() and atom2.lone_pairs == 1 and bond12.is_single():
                                bond12.set_order_num(2)
                                atom2.lone_pairs -= 1
                                atom2.charge += 1
                                atom.charge -= 1
                                break
                            elif atom2.is_nitrogen() and atom2.lone_pairs == 1 and bond12.is_single():
                                bond12.set_order_num(2)
                                atom2.lone_pairs -= 1
                                atom.lone_pairs += 1
                                atom2.charge += 1
                                atom.charge -= 1
                                break
                        else:
                            atom.lone_pairs += 1
                        atom.radical_electrons -= 2
    if len(mol.atoms) == 1 and mol.multiplicity == 1 and mol.atoms[0].radical_electrons == 4:
        # This is a singlet atomic C or Si, convert all radicals to lone pairs
        mol.atoms[0].radical_electrons = 0
        mol.atoms[0].lone_pairs = 2


def set_radicals_by_map(mol, radical_map):
    """
    Set radicals in ``mol`` by ``radical_map``.

    Args:
        mol (Molecule): The RMG Molecule object to process.
        radical_map (Molecule): An RMG Molecule object with the same atom order to be used as a radical map.

    Raises:
        ConverterError: If atom order does not match.
    """
    for i, atom in enumerate(mol.atoms):
        if atom.element.number != radical_map.atoms[i].element.number:
            raise ConverterError('Atom order in mol and radical_map in set_radicals_by_map() do not match. '
                                 '{0} is not {1}.'.format(atom.element.symbol, radical_map.atoms[i].symbol))
        atom.radical_electrons = radical_map.atoms[i].radical_electrons


def order_atoms_in_mol_list(ref_mol, mol_list):
    """
    Order the atoms in all molecules of ``mol_list`` by the atom order in ``ref_mol``.

    Args:
        ref_mol (Molecule): The reference Molecule object.
        mol_list (list): Entries are Molecule objects whos atoms will be reordered according to the reference.

    Returns:
        bool: Whether the reordering was successful, ``True`` if it was.

    Raises:
        SanitizationError: If atoms could not be re-ordered.
        TypeError: If ``ref_mol`` or the entries in ``mol_list`` have a wrong type.
    """
    if not isinstance(ref_mol, Molecule):
        raise TypeError(f'expected mol to be a Molecule instance, got {ref_mol} which is a {type(ref_mol)}.')
    if mol_list is not None:
        for mol in mol_list:
            if not isinstance(mol, Molecule):
                raise TypeError(f'expected enrties of mol_list to be Molecule instances, got {mol} '
                                f'which is a {type(mol)}.')
            try:  # TODO: flag as unordered (or solve)
                order_atoms(ref_mol, mol)
            except SanitizationError as e:
                logger.warning('Could not order atoms in\n{0}\nGot the following error:'
                               '\n{1}'.format(mol.to_adjacency_list, e))
                return False
    else:
        logger.warning('Could not order atoms')
        return False
    return True


def order_atoms(ref_mol, mol):
    """
    Order the atoms in ``mol`` by the atom order in ``ref_mol``.

    Args:
        ref_mol (Molecule): The reference Molecule object.
        mol (Molecule): The Molecule object to process.

    Raises:
        SanitizationError: If atoms could not be re-ordered.
        TypeError: If ``mol`` has a wrong type.
    """
    if not isinstance(mol, Molecule):
        raise TypeError(f'expected mol to be a Molecule instance, got {mol} which is a {type(mol)}.')
    if ref_mol is not None and mol is not None:
        ref_mol_is_iso_copy = ref_mol.copy(deep=True)
        mol_is_iso_copy = mol.copy(deep=True)
        ref_mol_find_iso_copy = ref_mol.copy(deep=True)
        mol_find_iso_copy = mol.copy(deep=True)

        ref_mol_is_iso_copy = update_molecule(ref_mol_is_iso_copy, to_single_bonds=True)
        mol_is_iso_copy = update_molecule(mol_is_iso_copy, to_single_bonds=True)
        ref_mol_find_iso_copy = update_molecule(ref_mol_find_iso_copy, to_single_bonds=True)
        mol_find_iso_copy = update_molecule(mol_find_iso_copy, to_single_bonds=True)

        if mol_is_iso_copy.is_isomorphic(ref_mol_is_iso_copy, save_order=True, strict=False):
            mapping = mol_find_iso_copy.find_isomorphism(ref_mol_find_iso_copy, save_order=True)
            if len(mapping):
                if isinstance(mapping, list):
                    mapping = mapping[0]
                index_map = {ref_mol_find_iso_copy.atoms.index(val): mol_find_iso_copy.atoms.index(key)
                             for key, val in mapping.items()}
                mol.atoms = [mol.atoms[index_map[i]] for i, _ in enumerate(mol.atoms)]
            else:
                # logger.debug('Could not map molecules {0}, {1}:\n\n{2}\n\n{3}'.format(
                #     ref_mol.copy(deep=True).to_smiles(), mol.copy(deep=True).to_smiles(),
                #     ref_mol.copy(deep=True).to_adjacency_list(), mol.copy(deep=True).to_adjacency_list()))
                raise SanitizationError('Could not map molecules')
        else:
            # logger.debug('Could not map non isomorphic molecules {0}, {1}:\n\n{2}\n\n{3}'.format(
            #     ref_mol.copy(deep=True).to_smiles(), mol.copy(deep=True).to_smiles(),
            #     ref_mol.copy(deep=True).to_adjacency_list(), mol.copy(deep=True).to_adjacency_list()))
            raise SanitizationError('Could not map non isomorphic molecules')


def update_molecule(mol, to_single_bonds=False):
    """
    Updates the molecule, useful for isomorphism comparison.

    Args:
        mol (Molecule): The RMG Molecule object to process.
        to_single_bonds (bool, optional): Whether to convert all bonds to single bonds. ``True`` to convert.

    Returns:
        Molecule: The updated molecule.
    """
    new_mol = Molecule()
    try:
        atoms = mol.atoms
    except AttributeError:
        return None
    atom_mapping = dict()
    for atom in atoms:
        new_atom = new_mol.add_atom(Atom(atom.element))
        atom_mapping[atom] = new_atom
    for atom1 in atoms:
        for atom2 in atom1.bonds.keys():
            bond_order = 1.0 if to_single_bonds else atom1.bonds[atom2].get_order_num()
            bond = Bond(atom_mapping[atom1], atom_mapping[atom2], bond_order)
            new_mol.add_bond(bond)
    try:
        new_mol.update_atomtypes()
    except (AtomTypeError, KeyError):
        pass
    new_mol.multiplicity = mol.multiplicity
    return new_mol


def s_bonds_mol_from_xyz(xyz):
    """
    Create a single bonded molecule from xyz using RMG's connect_the_dots() method.

    Args:
        xyz (dict): The xyz coordinates.

    Returns:
        Molecule: The respective molecule with only single bonds.
    """
    xyz = check_xyz_dict(xyz)
    mol = Molecule()
    for symbol, coord in zip(xyz['symbols'], xyz['coords']):
        atom = Atom(element=symbol)
        atom.coords = np.array([coord[0], coord[1], coord[2]], np.float64)
        mol.add_atom(atom)
    mol.connect_the_dots()  # only adds single bonds, but we don't care
    return mol


def to_rdkit_mol(mol, remove_h=False, sanitize=True):
    """
    Convert a molecular structure to an RDKit RDMol object. Uses
    `RDKit <http://rdkit.org/>`_ to perform the conversion.
    Perceives aromaticity.
    Adopted from rmgpy/molecule/converter.py

    Args:
        mol (Molecule): An RMG Molecule object for the conversion.
        remove_h (bool, optional): Whether to remove hydrogen atoms from the molecule, ``True`` to remove.
        sanitize (bool, optional): Whether to sanitize the RDKit molecule, ``True`` to sanitize.

    Returns:
        RDMol: An RDKit molecule object corresponding to the input RMG Molecule object.
    """
    atom_id_map = dict()

    # only manipulate a copy of ``mol``
    mol_copy = mol.copy(deep=True)
    if not mol_copy.atom_ids_valid():
        mol_copy.assign_atom_ids()
    for i, atom in enumerate(mol_copy.atoms):
        atom_id_map[atom.id] = i  # keeps the original atom order before sorting
    # mol_copy.sort_atoms()  # Sort the atoms before converting to ensure output is consistent between different runs
    atoms_copy = mol_copy.vertices

    rd_mol = Chem.rdchem.EditableMol(Chem.rdchem.Mol())
    for rmg_atom in atoms_copy:
        rd_atom = Chem.rdchem.Atom(rmg_atom.element.symbol)
        if rmg_atom.element.isotope != -1:
            rd_atom.SetIsotope(rmg_atom.element.isotope)
        rd_atom.SetNumRadicalElectrons(rmg_atom.radical_electrons)
        rd_atom.SetFormalCharge(rmg_atom.charge)
        if rmg_atom.element.symbol == 'C' and rmg_atom.lone_pairs == 1 and mol_copy.multiplicity == 1:
            # hard coding for carbenes
            rd_atom.SetNumRadicalElectrons(2)
        if not (remove_h and rmg_atom.symbol == 'H'):
            rd_mol.AddAtom(rd_atom)

    rd_bonds = Chem.rdchem.BondType
    orders = {'S': rd_bonds.SINGLE, 'D': rd_bonds.DOUBLE, 'T': rd_bonds.TRIPLE, 'B': rd_bonds.AROMATIC,
              'Q': rd_bonds.QUADRUPLE}
    # Add the bonds
    for atom1 in atoms_copy:
        for atom2, bond12 in atom1.edges.items():
            if bond12.is_hydrogen_bond():
                continue
            if atoms_copy.index(atom1) < atoms_copy.index(atom2):
                rd_mol.AddBond(atom_id_map[atom1.id], atom_id_map[atom2.id], orders[bond12.get_order_str()])

    # Make editable mol and rectify the molecule
    rd_mol = rd_mol.GetMol()
    if sanitize:
        Chem.SanitizeMol(rd_mol)
    if remove_h:
        rd_mol = Chem.RemoveHs(rd_mol, sanitize=sanitize)
    return rd_mol


def rdkit_conf_from_mol(mol, xyz):
    """
     Generate an RDKit Conformer object from an RMG Molecule object.

    Args:
        mol (Molecule): The RMG Molecule object.
        xyz (dict): The xyz coordinates (of the conformer, atoms must be ordered as in ``mol``.

    Returns:
        Conformer: An RDKit Conformer object.
    Returns:
        RDMol: An RDKit Molecule object.

    Raises:
        ConverterError: if ``xyz`` is of wrong type.
    """
    if not isinstance(xyz, dict):
        raise ConverterError('The xyz argument seem to be of wrong type. Expected a dictionary, '
                             'got\n{0}\nwhich is a {1}'.format(xyz, type(xyz)))
    rd_mol = to_rdkit_mol(mol=mol, remove_h=False)
    Chem.AllChem.EmbedMolecule(rd_mol)
    conf = None
    if rd_mol.GetNumConformers():
        conf = rd_mol.GetConformer(id=0)
        for i in range(rd_mol.GetNumAtoms()):
            conf.SetAtomPosition(i, xyz['coords'][i])  # reset atom coordinates
    return conf, rd_mol


def set_rdkit_dihedrals(conf, rd_mol, torsion, deg_increment=None, deg_abs=None):
    """
    A helper function for setting dihedral angles using RDKit.
    Either ``deg_increment`` or ``deg_abs`` must be specified.

    Args:
        conf: The RDKit conformer with the current xyz information.
        rd_mol: The respective RDKit molecule.
        torsion (list, tuple): The 0-indexed atom indices of the four atoms defining the torsion.
        deg_increment (float, optional): The required dihedral increment in degrees.
        deg_abs (float, optional): The required dihedral in degrees.

    Returns:
        dict: The xyz with the new dihedral, ordered according to the map.

    Raises:
        ConverterError: If the dihedral cannot be set.
    """
    if deg_increment is None and deg_abs is None:
        raise ConverterError('Cannot set dihedral without either a degree increment or an absolute degree')
    if deg_increment is not None:
        deg0 = rdMT.GetDihedralDeg(conf, torsion[0], torsion[1], torsion[2], torsion[3])  # get original dihedral
        deg = deg0 + deg_increment
    else:
        deg = deg_abs
    rdMT.SetDihedralDeg(conf, torsion[0], torsion[1], torsion[2], torsion[3], deg)
    coords = list()
    symbols = list()
    for i, atom in enumerate(list(rd_mol.GetAtoms())):
        coords.append([conf.GetAtomPosition(i).x, conf.GetAtomPosition(i).y, conf.GetAtomPosition(i).z])
        symbols.append(atom.GetSymbol())
    new_xyz = xyz_from_data(coords=coords, symbols=symbols)
    return new_xyz


def check_isomorphism(mol1, mol2, filter_structures=True):
    """
    Convert ``mol1`` and ``mol2`` to RMG Species objects, and generate resonance structures.
    Then check Species isomorphism.
    This function first makes copies of the molecules, since isIsomorphic() changes atom orders.

    Args:
        mol1 (Molecule): An RMG Molecule object.
        mol2 (Molecule): An RMG Molecule object.
        filter_structures (bool, optional): Whether to apply the filtration algorithm when generating
                                            resonance structures. ``True`` to apply.

    Returns:
        bool: Whether one of the molecules in the Species derived from ``mol1``
              is isomorphic to one of the molecules in the Species derived from ``mol2``. ``True`` if it is.
    """
    mol1.reactive, mol2.reactive = True, True
    mol1_copy = mol1.copy(deep=True)
    mol2_copy = mol2.copy(deep=True)
    spc1 = Species(molecule=[mol1_copy])
    spc2 = Species(molecule=[mol2_copy])

    try:
        spc1.generate_resonance_structures(keep_isomorphic=False, filter_structures=filter_structures)
    except AtomTypeError:
        pass
    try:
        spc2.generate_resonance_structures(keep_isomorphic=False, filter_structures=filter_structures)
    except AtomTypeError:
        pass

    for molecule1 in spc1.molecule:
        for molecule2 in spc2.molecule:
            if molecule1.is_isomorphic(molecule2, save_order=True):
                return True
    return False


def get_center_of_mass(xyz):
    """
    Get the center of mass of xyz coordinates.
    Assumes arc.converter.standardize_xyz_string() was already called for xyz.
    Note that xyz from ESS output is usually already centered at the center of mass (to some precision).

    Args:
        xyz (dict): The xyz coordinates.

    Returns:
        tuple: The center of mass coordinates.
    """
    masses = [get_element_mass(symbol, isotope)[0] for symbol, isotope in zip(xyz['symbols'], xyz['isotopes'])]
    cm_x, cm_y, cm_z = 0, 0, 0
    for coord, mass in zip(xyz['coords'], masses):
        cm_x += coord[0] * mass
        cm_y += coord[1] * mass
        cm_z += coord[2] * mass
    cm_x /= sum(masses)
    cm_y /= sum(masses)
    cm_z /= sum(masses)
    return cm_x, cm_y, cm_z


def translate_to_center_of_mass(xyz):
    """
    Translate coordinates to their center of mass.

    Args:
        xyz (dict): The 3D coordinates.

    Returns:
        dict: The translated coordinates.
    """
    # identify and remove dummy atoms for center of mass determination (but then do translate dummy atoms as well)
    dummies = list()
    for i, symbol in enumerate(xyz['symbols']):
        if symbol == 'X':
            dummies.append(i)
    no_dummies_xyz = {'symbols': [symbol for i, symbol in enumerate(xyz['symbols']) if i not in dummies],
                      'isotopes': [isotope for i, isotope in enumerate(xyz['isotopes']) if i not in dummies],
                      'coords': [coord for i, coord in enumerate(xyz['coords']) if i not in dummies]}
    cm_x, cm_y, cm_z = get_center_of_mass(no_dummies_xyz)
    x = [coord[0] - cm_x for coord in xyz['coords']]
    y = [coord[1] - cm_y for coord in xyz['coords']]
    z = [coord[2] - cm_z for coord in xyz['coords']]
    for i in range(len(x)):
        x[i] = x[i] if abs(x[i]) > 1e-10 else 0.0
        y[i] = y[i] if abs(y[i]) > 1e-10 else 0.0
        z[i] = z[i] if abs(z[i]) > 1e-10 else 0.0
    translated_coords = tuple((xi, yi, zi) for xi, yi, zi in zip(x, y, z))
    return xyz_from_data(coords=translated_coords, symbols=xyz['symbols'], isotopes=xyz['isotopes'])


def get_xyz_radius(xyz):
    """
    Determine the largest distance from the coordinate system origin attributed to one of the atoms in 3D space.

    Returns:
        float: The radius in Angstrom.
    """
    translated_xyz = translate_to_center_of_mass(xyz)
    border_elements = list()  # a list of the farthest element/s
    r = 0
    x, y, z = xyz_to_x_y_z(translated_xyz)
    for si, xi, yi, zi in zip(translated_xyz['symbols'], x, y, z):
        ri = xi ** 2 + yi ** 2 + zi ** 2
        if ri == r:
            border_elements.append(si)
        elif ri > r:
            r = ri
            border_elements = [si]
    # also consider the atom's radius:
    atom_r = max([get_atom_radius(si) if get_atom_radius(si) is not None else 1.50 for si in border_elements])
    radius = r ** 0.5 + atom_r
    return radius
